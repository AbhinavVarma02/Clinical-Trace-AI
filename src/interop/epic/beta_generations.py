"""SQLite-authorized MOCK generations, immutable evidence and persistent clinical pins."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from src.errors import NotFoundError
from src.governance.review import artifact_content_from_run, artifact_hash
from src.interop.epic.beta_acquisition import Acquisition, AcquisitionError, object_json
from src.interop.epic.beta_catalog import MOCK_PATIENTS, POLICY_VERSION, Catalog, RefreshRejected
from src.interop.epic.beta_staging import MANIFEST_VERSION, SOURCE, encoded, normalize
from src.interop.epic.mapping import MAPPING_VERSION, content_sha256, internal_id
from src.persistence.db import dumps
from src.safety.policy import AccessContext, Persona

if TYPE_CHECKING:
    from src.runtime import Runtime

GENERATION_ID = re.compile(r"[a-f0-9]{32}")
FRESH_S = 86400
CONTINUED_S = 7 * FRESH_S
HISTORY_S = 30 * FRESH_S
STORAGE_BYTES = 256 * 1024 * 1024
Hook = Callable[[str, str], Awaitable[None]]


def _plain(path: Path) -> None:
    """Reject links, junctions and other reparse points before reading evidence or state."""
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise RefreshRejected(409, "generation_integrity")


def _sync_directory(path: Path) -> None:
    # Windows has no portable directory fsync. File fsync and process-crash tests
    # establish the local boundary, without claiming power-loss durability.
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _write(path: Path, data: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


class Generations:
    def __init__(self, catalog: Catalog, root: Path, hook: Hook | None = None) -> None:
        self.catalog, self.hook = catalog, hook
        self.root = root.absolute()
        self.runtimes: dict[str, Runtime] = {}
        self.invalid: set[str] = set()
        # Storage is an explicit sibling of this MOCK control database, on its volume.
        if (
            self.root.parent.resolve()
            != Path(catalog.db.execute("PRAGMA database_list").fetchone()[2]).parent.resolve()
        ):
            raise ValueError("MOCK generation storage must share the control catalog directory")
        self.root.mkdir(mode=0o700, exist_ok=True)
        _plain(self.root)
        for name in ("staging", "objects", "state"):
            path = self.root / name
            path.mkdir(mode=0o700, exist_ok=True)
            _plain(path)
        catalog.db.execute("PRAGMA synchronous=FULL")
        catalog.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS generation (
                generation_id TEXT PRIMARY KEY, alias TEXT NOT NULL, attempt_id TEXT UNIQUE NOT NULL,
                manifest_hash TEXT NOT NULL, retrieved_at REAL NOT NULL, promoted_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS clinical_pin (
                identifier TEXT PRIMARY KEY, generation_id TEXT NOT NULL, alias TEXT NOT NULL,
                kind TEXT NOT NULL, created_at REAL NOT NULL, content_hash TEXT
            );
            CREATE TABLE IF NOT EXISTS generation_store (singleton INTEGER PRIMARY KEY, root_hash TEXT NOT NULL);
            """
        )
        root_hash = hashlib.sha256(str(self.root.resolve()).encode()).hexdigest()
        with catalog.db:
            catalog.db.execute("INSERT OR IGNORE INTO generation_store VALUES (1, ?)", (root_hash,))
        if catalog.db.execute("SELECT root_hash FROM generation_store").fetchone()[0] != root_hash:
            raise ValueError("MOCK generation store does not match its catalog")

    def close(self) -> None:
        for runtime in self.runtimes.values():
            if not runtime.close():
                raise RuntimeError("clinical runtime did not drain")
        self.runtimes.clear()

    def quota(self) -> None:
        total = 0
        for path in self.root.rglob("*"):
            _plain(path)
            if path.is_file():
                total += path.stat().st_size
                if total + 20 * 1024 * 1024 > STORAGE_BYTES:
                    raise RefreshRejected(429, "storage_quota")

    async def phase(self, point: str, gid: str) -> None:
        if self.hook is not None:
            await self.hook(point, gid)

    def _head(self, alias: str) -> dict[str, Any]:
        row = self.catalog.db.execute("SELECT * FROM patient_head WHERE alias = ?", (alias,)).fetchone()
        return (
            dict(row)
            if row
            else {"alias": alias, "active": None, "epoch": 0, "revision": 0, "last_success": None, "rolled_back": 0}
        )

    def _row(self, alias: str, gid: str) -> dict[str, Any]:
        if not GENERATION_ID.fullmatch(gid):
            raise RefreshRejected(404, "generation_unavailable")
        row = self.catalog.db.execute(
            "SELECT * FROM generation WHERE generation_id = ? AND alias = ?", (gid, alias)
        ).fetchone()
        if row is None:
            raise RefreshRejected(404, "generation_unavailable")
        return dict(row)

    def _files(self, gid: str) -> tuple[Path, Path]:
        for name in ("objects", "state"):
            _plain(self.root / name)
            _plain(self.root / name / gid)
        directory = self.root / "objects" / gid
        bundle = directory / "normalized_bundle.json"
        manifest = directory / "manifest.json"
        for path in (bundle, manifest, self.root / "state" / gid / "clinical.sqlite"):
            _plain(path)
        return manifest, bundle

    def evidence(self, alias: str, gid: str) -> dict[str, Any]:
        row = self._row(alias, gid)
        if gid in self.invalid:
            raise RefreshRejected(409, "generation_integrity")
        try:
            _plain(self.root)
            manifest_path, bundle_path = self._files(gid)
            manifest_bytes = manifest_path.read_bytes()
            bundle_bytes = bundle_path.read_bytes()
            if content_sha256(manifest_bytes) != row["manifest_hash"]:
                raise ValueError
            manifest = object_json(manifest_bytes)
            if (
                manifest.get("generation_id") != gid
                or manifest.get("alias") != alias
                or manifest.get("source") != SOURCE
                or manifest.get("mode") != "MOCK"
                or manifest.get("patient_id") != internal_id("Patient", MOCK_PATIENTS[alias])
                or manifest.get("manifest_version") != MANIFEST_VERSION
                or manifest.get("mapping_version") != MAPPING_VERSION
                or manifest.get("bundle_sha256") != content_sha256(bundle_bytes)
            ):
                raise ValueError
            return manifest
        except (OSError, ValueError, AcquisitionError, RefreshRejected):
            self.invalid.add(gid)
            raise RefreshRejected(409, "generation_integrity") from None

    def runtime(self, alias: str, gid: str) -> tuple[Runtime, dict[str, Any]]:
        manifest = self.evidence(alias, gid)
        try:
            from src.sources import tracing_enabled

            if tracing_enabled():
                raise ValueError
            if gid not in self.runtimes:
                from src.runtime import build_runtime

                state_path = self.root / "state" / gid / "clinical.sqlite"
                with sqlite3.connect(state_path.as_uri() + "?mode=ro", uri=True) as connection:
                    connection.row_factory = sqlite3.Row
                    self._check_rows(
                        connection.execute("SELECT * FROM fhir_resource").fetchall(),
                        self.root / "objects" / gid / "normalized_bundle.json",
                        manifest["patient_id"],
                    )
                runtime = build_runtime(
                    self.root / "state" / gid / "clinical.sqlite",
                    fixture=self.root / "objects" / gid / "normalized_bundle.json",
                    provider=None,
                    enable_model=False,
                )
                self.runtimes[gid] = runtime
            runtime = self.runtimes[gid]
            self._check_runtime(runtime, self.root / "objects" / gid / "normalized_bundle.json", manifest["patient_id"])
            runtime.records.source_coverage = self.source_coverage(manifest)
            return runtime, manifest
        except (OSError, ValueError, TypeError, KeyError, sqlite3.DatabaseError):
            self.invalid.add(gid)
            raise RefreshRejected(409, "generation_integrity") from None

    @staticmethod
    def _check_rows(rows: Any, bundle: Path, patient: str) -> None:
        from src.clinical.store import content_hash

        resources = [item["resource"] for item in object_json(bundle.read_bytes())["entry"]]
        expected = {(r["resourceType"], r["id"]): r for r in resources}
        actual = {(row["resource_type"], row["resource_id"]): json.loads(row["payload"]) for row in rows}
        if actual != expected or len(rows) != len(expected):
            raise ValueError("generation runtime does not match immutable evidence")
        if any(
            row["patient_id"] != patient
            or row["version_id"] != 1
            or row["is_current"] != 1
            or row["content_hash"] != content_hash(actual[row["resource_type"], row["resource_id"]])
            for row in rows
        ):
            raise ValueError("generation runtime identity does not match immutable evidence")

    @staticmethod
    def _check_runtime(runtime: Runtime, bundle: Path, patient: str) -> None:
        Generations._check_rows(runtime.db.all("SELECT * FROM fhir_resource"), bundle, patient)
        if runtime.store.patient_ids() != [patient] or runtime.startup_ingestion["invalid"]:
            raise ValueError("generation runtime patient mismatch")
        if runtime.provider is not None or runtime.model_available:
            raise ValueError("MOCK runtime must be deterministic")

    def _fence(self, acquisition: Acquisition, alias: str) -> tuple[int, int]:
        acquisition.checkpoint()
        row = self.catalog.db.execute(
            "SELECT f.epoch, f.revision FROM attempt_fence f JOIN attempt a USING(attempt_id) "
            "JOIN patient_head h ON h.alias = a.alias WHERE a.attempt_id = ? AND a.alias = ? "
            "AND a.state = 'running' AND f.epoch = h.epoch AND f.revision = h.revision",
            (acquisition.attempt, alias),
        ).fetchone()
        if row is None:
            raise AcquisitionError("publication_fenced")
        return row["epoch"], row["revision"]

    async def publish(self, acquisition: Acquisition, alias: str) -> str:
        # This store and its browser readers remain exclusively MOCK in Increment 1.
        if type(acquisition) is not Acquisition or acquisition.mode != "MOCK" or acquisition.policy != POLICY_VERSION:
            raise AcquisitionError("live_publication_disabled")
        epoch, revision = self._fence(acquisition, alias)
        self.quota()
        gid = uuid.uuid4().hex
        staging = self.root / "staging" / gid
        final = self.root / "objects" / gid
        state = self.root / "state" / gid
        staging.mkdir(mode=0o700)
        await self.phase("staging", gid)
        manifest, bundle = normalize(acquisition, alias, gid)
        _write(staging / "normalized_bundle.json", bundle)
        manifest_bytes = encoded(manifest)
        if len(bundle) + len(manifest_bytes) > 20 * 1024 * 1024:
            raise AcquisitionError("byte_limit")
        _write(staging / "manifest.json", manifest_bytes)
        _sync_directory(staging)
        state.mkdir(mode=0o700)
        from src.runtime import build_runtime
        from src.sources import tracing_enabled

        if tracing_enabled():
            raise AcquisitionError("external_tracing_enabled")
        runtime = build_runtime(
            state / "clinical.sqlite", fixture=staging / "normalized_bundle.json", provider=None, enable_model=False
        )
        try:
            self._check_runtime(runtime, staging / "normalized_bundle.json", manifest["patient_id"])
            runtime.records.source_coverage = self.source_coverage(manifest)
            runtime.index.search(manifest["patient_id"], "record", k=1)
        finally:
            runtime.close()
        # Flush the closed SQLite state before the catalog can name the generation.
        with (state / "clinical.sqlite").open("r+b") as stream:
            os.fsync(stream.fileno())
        _sync_directory(state)
        await self.phase("before_finalize", gid)
        self._fence(acquisition, alias)
        if content_sha256((staging / "normalized_bundle.json").read_bytes()) != manifest["bundle_sha256"]:
            raise AcquisitionError("generation_integrity")
        if (staging / "manifest.json").read_bytes() != manifest_bytes:
            raise AcquisitionError("generation_integrity")
        os.rename(staging, final)
        _sync_directory(self.root / "objects")
        await self.phase("after_finalize", gid)
        await self.phase("before_publish", gid)
        _plain(final)
        for name in ("normalized_bundle.json", "manifest.json"):
            _plain(final / name)
        if (final / "normalized_bundle.json").read_bytes() != bundle or (
            final / "manifest.json"
        ).read_bytes() != manifest_bytes:
            raise AcquisitionError("generation_integrity")
        _plain(state)
        _plain(state / "clinical.sqlite")
        try:
            with sqlite3.connect((state / "clinical.sqlite").as_uri() + "?mode=ro", uri=True) as connection:
                connection.row_factory = sqlite3.Row
                self._check_rows(
                    connection.execute("SELECT * FROM fhir_resource").fetchall(),
                    final / "normalized_bundle.json",
                    manifest["patient_id"],
                )
        except (OSError, ValueError, KeyError, sqlite3.DatabaseError):
            raise AcquisitionError("generation_integrity") from None
        self._fence(acquisition, alias)
        # No await between authorization/deadline/fence validation and the pointer transaction.
        with self.catalog.db:
            self.catalog.db.execute(
                "INSERT INTO generation VALUES (?, ?, ?, ?, ?, ?)",
                (
                    gid,
                    alias,
                    acquisition.attempt,
                    content_sha256(manifest_bytes),
                    manifest["retrieved_at"],
                    self.catalog.clock(),
                ),
            )
            updated = self.catalog.db.execute(
                "UPDATE patient_head SET active = ?, last_success = ?, rolled_back = 0, epoch = epoch + 1 "
                "WHERE alias = ? AND epoch = ? AND revision = ?",
                (gid, gid, alias, epoch, revision),
            )
            if updated.rowcount != 1:
                raise AcquisitionError("publication_fenced")
            self.catalog.db.execute(
                "UPDATE attempt SET state = 'succeeded', finished_at = ?, operations = ? WHERE attempt_id = ?",
                (self.catalog.clock(), json.dumps(acquisition.operations), acquisition.attempt),
            )
            self.catalog._audit("generation_published", acquisition.attempt)
        await self.phase("after_publish", gid)
        return gid

    def serving(self, alias: str, gid: str, manifest: dict[str, Any]) -> str:
        now = self.catalog.clock()
        age = now - manifest["retrieved_at"]
        if age < 0 or age > CONTINUED_S:
            return "unavailable"
        head = self._head(alias)
        if head["active"] == gid and head["rolled_back"]:
            return "rolled_back"
        if age > FRESH_S:
            return "stale"
        latest = self.catalog.status(alias)["attempt"]
        if head["active"] == gid and latest and latest["attempt_id"] == manifest["attempt_id"]:
            return "fresh"
        return "cached"

    def status(self, alias: str) -> dict[str, Any]:
        result = self.catalog.status(alias)
        head = self._head(alias)
        result.update(active=None, last_successful_generation=head["last_success"], publication_epoch=head["epoch"])
        if head["active"] is not None:
            gid = head["active"]
            row = self._row(alias, gid)
            result["active"] = {
                "generation_id": gid,
                "retrieved_at": row["retrieved_at"],
                "promoted_at": row["promoted_at"],
            }
            try:
                _, manifest = self.runtime(alias, gid)
                result["serving"] = self.serving(alias, gid, manifest)
            except RefreshRejected:
                result["serving"] = "unavailable"
                result["evidence_error"] = "generation_integrity"
        return result

    def select(self, alias: str, gid: str, acknowledgement: bool, *, explicit: bool = False) -> dict[str, Any]:
        _, manifest = self.runtime(alias, gid)
        state = self.serving(alias, gid, manifest)
        if state == "unavailable":
            raise RefreshRejected(410, "generation_expired")
        if state != "fresh" and not acknowledgement:
            raise RefreshRejected(409, "cache_acknowledgement_required")
        row = self._row(alias, gid)
        if explicit:
            event = "generation_selected" if state == "fresh" else "cached_generation_selected"
            self.catalog.audit(event, row["attempt_id"])
        return {
            "generation_id": gid,
            "source": SOURCE,
            "patient_alias": alias,
            "mode": "MOCK",
            "serving": state,
            "retrieved_at": manifest["retrieved_at"],
            "coverage": manifest["coverage"],
            "promoted_at": row["promoted_at"],
        }

    async def rollback(self, alias: str, gid: str, authorized: Callable[[], bool]) -> dict[str, Any]:
        # A new rollback requires a recently issued, authenticated owner session at the route.
        self.select(alias, gid, True)
        head = self._head(alias)
        await self.phase("before_rollback", gid)
        if not authorized():
            raise RefreshRejected(401, "owner_required")
        manifest = self.evidence(alias, gid)
        with self.catalog.db:
            changed = self.catalog.db.execute(
                "UPDATE patient_head SET active = ?, rolled_back = 1, epoch = epoch + 1 "
                "WHERE alias = ? AND epoch = ? AND revision = ?",
                (gid, alias, head["epoch"], head["revision"]),
            )
            if changed.rowcount != 1:
                raise RefreshRejected(409, "publication_fenced")
            self.catalog._audit("generation_rolled_back", manifest["attempt_id"])
        return self.status(alias)

    @staticmethod
    def access(alias: str, gid: str, patient: str) -> AccessContext:
        return AccessContext(
            Persona("mock-owner", "Authenticated MOCK owner", "clinician", (patient,)),
            workspace=f"{SOURCE}/{alias}/{gid}",
        )

    @staticmethod
    def context(manifest: dict[str, Any]) -> dict[str, Any]:
        return {
            key: manifest[key]
            for key in (
                "source",
                "alias",
                "generation_id",
                "patient_id",
                "bundle_sha256",
                "mapping_version",
                "policy",
                "retrieved_at",
            )
        } | {"manifest_sha256": content_sha256(encoded(manifest))}

    def pin_run(self, alias: str, gid: str, acknowledgement: bool) -> tuple[Runtime, dict[str, Any], str]:
        self.select(alias, gid, acknowledgement)
        runtime, manifest = self.runtime(alias, gid)
        identifier = str(uuid.uuid4())
        with self.catalog.db:
            self.catalog.db.execute(
                "INSERT INTO clinical_pin VALUES (?, ?, ?, 'run', ?, NULL)",
                (identifier, gid, alias, self.catalog.clock()),
            )
        return runtime, manifest, identifier

    def execute(self, runtime: Runtime, manifest: dict[str, Any], identifier: str, question: str) -> dict[str, Any]:
        from src.sources import source_provenance

        access = self.access(manifest["alias"], manifest["generation_id"], manifest["patient_id"])
        result = runtime.agent.run(access, manifest["patient_id"], question, run_id=identifier)
        context = self.context(manifest)
        result["evidence_context"] = context
        result["coverage"] = manifest["coverage"]
        for evidence in result["evidence"]:
            evidence["evidence_context"] = context
            kind, _, rid = evidence["source_ref"].partition("/")
            resource = runtime.store.get(kind, rid, int(evidence["source_version"]))
            evidence["provenance"] = source_provenance(resource) if resource is not None else None
            if resource is None:
                raise ValueError("citation evidence missing")
        result["review"]["required"] = True
        result["review"]["reasons"] = sorted(
            set([*result["review"].get("reasons", []), "independent_reviewer_required"])
        )
        runtime.db.execute("UPDATE agent_run SET result = ? WHERE run_id = ?", (dumps(result), identifier))
        return result

    def present(self, alias: str, gid: str, result: dict[str, Any]) -> dict[str, Any]:
        manifest = self.evidence(alias, gid)
        return {**result, "serving": self.serving(alias, gid, manifest)}

    def finish_run(self, identifier: str, result: dict[str, Any]) -> None:
        with self.catalog.db:
            self.catalog.db.execute(
                "UPDATE clinical_pin SET content_hash = ? WHERE identifier = ? AND kind = 'run'",
                (content_sha256(encoded(result)), identifier),
            )

    def pinned(self, alias: str, gid: str, identifier: str, kind: str) -> tuple[Runtime, dict[str, Any]]:
        row = self.catalog.db.execute(
            "SELECT * FROM clinical_pin WHERE identifier = ? AND generation_id = ? AND alias = ? AND kind = ?",
            (identifier, gid, alias, kind),
        ).fetchone()
        if row is None:
            raise RefreshRejected(404, "artifact_unavailable")
        if self.catalog.clock() - row["created_at"] > HISTORY_S:
            raise RefreshRejected(410, "artifact_expired")
        runtime, manifest = self.runtime(alias, gid)
        access = self.access(alias, gid, manifest["patient_id"])
        if kind == "run":
            content = runtime.agent.get_run(access, identifier)
            if content is None:
                raise RefreshRejected(409, "run_interrupted")
            if content.get("evidence_context") != self.context(manifest):
                raise RefreshRejected(409, "generation_integrity")
        else:
            try:
                content = runtime.reviews.get(access, identifier)
            except (NotFoundError, TypeError, ValueError):
                raise RefreshRejected(409, "generation_integrity") from None
            artifact = content.get("artifact")
            if (
                not artifact
                or not isinstance(artifact["content"], dict)
                or artifact["content"].get("evidence_context") != self.context(manifest)
            ):
                raise RefreshRejected(409, "generation_integrity")
            if artifact_hash(artifact["content"]) != artifact["content_hash"]:
                raise RefreshRejected(409, "generation_integrity")
        if row["content_hash"] != content_sha256(encoded(content)):
            raise RefreshRejected(409, "generation_integrity")
        return runtime, content

    @staticmethod
    def source_coverage(manifest: dict[str, Any]) -> dict[str, str]:
        from src.clinical.records import is_discharge_summary

        metadata = manifest["document_metadata"]
        return {
            "MedicationRequest": "not_imported",
            "DocumentReference": "not_imported",
            "DocumentReference.metadata": "returned" if metadata else "returned_none",
            "DocumentReference.discharge_summary_metadata": (
                "returned" if any(is_discharge_summary(document) for document in metadata) else "returned_none"
            ),
        }

    def submit_review(self, alias: str, gid: str, identifier: str, acknowledgement: bool = False) -> dict[str, Any]:
        self.select(alias, gid, acknowledgement)
        runtime, run = self.pinned(alias, gid, identifier, "run")
        access = self.access(alias, gid, run["patient_id"])
        if run["outcome"] != "answered":
            raise RefreshRejected(409, "answered_run_required")
        existing = runtime.db.all("SELECT review_id FROM review WHERE artifact_id = ? LIMIT 2", (identifier,))
        if len(existing) > 1:
            raise RefreshRejected(409, "generation_integrity")
        try:
            # A persisted review must be checked even if its artifact has gone missing.
            review = (
                runtime.reviews.get(access, existing[0]["review_id"])
                if existing
                else runtime.reviews.submit_run(access, run)
            )
        except (NotFoundError, TypeError, ValueError, sqlite3.IntegrityError):
            raise RefreshRejected(409, "generation_integrity") from None
        artifact = review["artifact"]
        expected_hash = artifact_hash(artifact_content_from_run(run))
        if (
            not artifact
            or review["patient_id"] != run["patient_id"]
            or review["status"] != "pending_review"
            or review["artifact_version"] != 1
            or review["artifact_hash"] != expected_hash
            or artifact["content_hash"] != expected_hash
            or artifact_hash(artifact["content"]) != expected_hash
        ):
            raise RefreshRejected(409, "generation_integrity")
        digest = content_sha256(encoded(review))
        with self.catalog.db:
            pin = self.catalog.db.execute(
                "SELECT generation_id, alias, kind, content_hash FROM clinical_pin WHERE identifier = ?",
                (review["review_id"],),
            ).fetchone()
            if pin is None:
                self.catalog.db.execute(
                    "INSERT INTO clinical_pin VALUES (?, ?, ?, 'review', ?, ?)",
                    (review["review_id"], gid, alias, self.catalog.clock(), digest),
                )
            elif tuple(pin) != (gid, alias, "review", digest):
                raise RefreshRejected(409, "generation_integrity")
        return review
