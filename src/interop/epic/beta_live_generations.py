"""Owner-approved, evidence-only LIVE generations: hash-bound, atomic promotion of one staged candidate.

A generation is ``objects/<gid>/{manifest.json, normalized_bundle.json}``, re-verified on every load. It has no
runtime state, no clinical pins and no cached runtime; clinical AI, browser exposure and external publication are
not part of it. This is a separate store from the MOCK ``Generations`` (own table, own directory), so none of the
MOCK patient-isolation checks is touched or loosened.

Tier isolation: the tier is fixed by this store, never read from a manifest. A manifest must match exactly (mode
LIVE, the Epic source, the LIVE policy and manifest version, the unconfirmed-completeness marker, this alias and
the patient behind it in the immutable allowlist); a MOCK or fake-transport manifest cannot pass.

Promotion is two explicit owner actions (stage, then approve) and never automatic. It validates the new generation
completely, then switches the pointer and marks the previous generation for erasure in one catalog transaction, so
a failure at any point leaves the previous generation intact. Real (non-fixture) data cannot be promoted until
genuine owner authentication exists: ``real_promotion`` is False and nothing in the code sets it.
"""

from __future__ import annotations

import os
import re
import secrets
from pathlib import Path
from typing import Any, Callable

from src.interop.epic import beta_live_staging as files
from src.interop.epic import settings
from src.interop.epic.beta_acquisition import AcquisitionError, object_json
from src.interop.epic.beta_catalog import LIVE_POLICY_VERSION, Catalog, RefreshRejected
from src.interop.epic.beta_generations import CONTINUED_S, FRESH_S, Generations
from src.interop.epic.beta_live_acquisition import COMPLETENESS, LiveAcquisition
from src.interop.epic.beta_live_staging import LiveAllowlist, LiveStaging, review_summary
from src.interop.epic.mapping import MAPPING_VERSION, content_sha256, internal_id

GENERATION_RETENTION_S = 30 * 24 * 3600
GENERATION_ID = re.compile(r"[a-f0-9]{32}")
CANDIDATE_HASH = re.compile(r"[a-f0-9]{64}")
Hook = Callable[[str], None]


def _refusal(exc: AcquisitionError) -> RefreshRejected:
    """Owner-facing status for a refusal; only the fixed code is exposed."""
    status = {"acl_unverified": 503, "candidate_expired": 410}.get(exc.code)
    if status is None:
        status = 404 if exc.code in {"candidate_unavailable", "staging_scope", "authorization_revoked"} else 409
    return RefreshRejected(status, exc.code)


def check_tier(manifest: dict[str, Any], alias: str, patient: str | None, bundle: bytes) -> None:
    """Exact LIVE-tier provenance check. Raises ValueError when anything differs."""
    expected = {
        "mode": "LIVE",
        "source": settings.SOURCE_SYSTEM,
        "policy": LIVE_POLICY_VERSION,
        "manifest_version": LiveAcquisition.manifest_version,
        "mapping_version": MAPPING_VERSION,
        "completeness": COMPLETENESS,
        "alias": alias,
    }
    if any(manifest.get(key) != value for key, value in expected.items()):
        raise ValueError("tier mismatch")
    if patient is None or manifest.get("patient_id") != internal_id("Patient", patient):
        raise ValueError("patient mismatch")
    if manifest.get("bundle_sha256") != content_sha256(bundle):
        raise ValueError("bundle mismatch")
    if (
        not GENERATION_ID.fullmatch(str(manifest.get("generation_id")))
        or type(manifest.get("synthetic_transport")) is not bool
    ):
        raise ValueError("manifest shape")


class LiveGenerations:
    def __init__(
        self,
        catalog: Catalog,
        root: Path,
        allowlist: LiveAllowlist,
        staging: LiveStaging,
        *,
        hook: Hook | None = None,
        real_promotion: bool = False,
    ) -> None:
        self.catalog, self.allowlist, self.staging, self.hook = catalog, allowlist, staging, hook
        self.real_promotion = real_promotion  # False until genuine owner authentication exists; never set in code
        self.root = files.private_root(root)
        for name in ("objects", "tmp"):
            path = self.root / name
            path.mkdir(mode=0o700, exist_ok=True)
            if files._is_link(path.lstat()):
                raise ValueError("LIVE generation storage must not contain links")
        with self.catalog.db:
            self.catalog.db.execute(
                """
                CREATE TABLE IF NOT EXISTS live_generation (
                    generation_id TEXT PRIMARY KEY, alias TEXT NOT NULL, attempt_id TEXT UNIQUE NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('approved','erase_pending','erased')),
                    manifest_sha256 TEXT, retrieved_at REAL, approved_at REAL NOT NULL, expires_at REAL NOT NULL,
                    erased_at REAL, erase_reason TEXT
                )
                """
            )
            self.catalog.db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS live_generation_one_approved ON live_generation (alias) "
                "WHERE state = 'approved'"
            )
        self.sweep()

    def _hook(self, point: str) -> None:
        if self.hook is not None:
            self.hook(point)

    # -- verification and queries --------------------------------------------------------------------------

    def _evidence(self, row: Any) -> dict[str, Any]:
        """Manifest of an approved generation, re-verified against this tier and its stored hash."""
        gid, alias = row["generation_id"], row["alias"]
        try:
            directory = self.root / "objects" / gid
            for path in (self.root, self.root / "objects", directory):
                if files._is_link(path.lstat()):
                    raise ValueError
            manifest_path, bundle_path = directory / "manifest.json", directory / "normalized_bundle.json"
            if files._is_link(manifest_path.lstat()) or files._is_link(bundle_path.lstat()):
                raise ValueError
            manifest_bytes, bundle_bytes = manifest_path.read_bytes(), bundle_path.read_bytes()
            if content_sha256(manifest_bytes) != row["manifest_sha256"]:
                raise ValueError
            manifest = object_json(manifest_bytes)
            check_tier(manifest, alias, self.allowlist.get(alias), bundle_bytes)
            if manifest.get("generation_id") != gid:
                raise ValueError
            return manifest
        except (OSError, ValueError, TypeError, KeyError, AcquisitionError):
            raise RefreshRejected(409, "generation_integrity") from None

    def view(self, alias: str) -> dict[str, Any] | None:
        """Content-free description of the alias's generation, or None."""
        row = self.catalog.db.execute(
            "SELECT * FROM live_generation WHERE alias = ? AND state IN ('approved','erase_pending') "
            "ORDER BY state = 'approved' DESC, approved_at DESC LIMIT 1",
            (alias,),
        ).fetchone()
        if row is None:
            return None
        result: dict[str, Any] = {
            "generation_id": row["generation_id"],
            "state": row["state"],
            "approved_at": row["approved_at"],
            "expires_at": row["expires_at"],
            "retrieved_at": row["retrieved_at"],
            "freshness": "unavailable",
            "review": None,
        }
        if row["state"] != "approved":
            return result
        try:
            manifest = self._evidence(row)
        except RefreshRejected:
            return {**result, "evidence_error": "generation_integrity"}
        age = self.catalog.clock() - manifest["retrieved_at"]
        if 0 <= age <= CONTINUED_S:
            latest = self.catalog.status(alias, live=True)["attempt"]
            fresh = age <= FRESH_S and latest is not None and latest["attempt_id"] == row["attempt_id"]
            result["freshness"] = "fresh" if fresh else ("cached" if age <= FRESH_S else "stale")
        result["review"] = review_summary(manifest)
        return result

    # -- promotion -----------------------------------------------------------------------------------------

    def _admit(self, alias: str, authorized: Callable[[], bool]) -> None:
        if self.catalog.revoked("owner") or self.catalog.revoked(alias):
            raise RefreshRejected(404, "patient_unavailable")
        if not authorized():
            raise RefreshRejected(401, "owner_required")

    def _fence(self, attempt: str, alias: str) -> tuple[int, int]:
        row = self.catalog.db.execute(
            "SELECT f.epoch, f.revision FROM attempt_fence f JOIN attempt a USING (attempt_id) "
            "JOIN patient_head h ON h.alias = a.alias WHERE a.attempt_id = ? AND a.alias = ? "
            "AND a.state = 'succeeded' AND a.policy = ? AND f.epoch = h.epoch AND f.revision = h.revision",
            (attempt, alias, LIVE_POLICY_VERSION),
        ).fetchone()
        if row is None:
            raise RefreshRejected(409, "publication_fenced")
        return row["epoch"], row["revision"]

    def _ingest_check(self, gid: str, manifest: dict[str, Any], bundle: bytes) -> None:
        """Prove the bundle loads and matches its evidence, in a throwaway directory that is always erased."""
        from src.runtime import build_runtime
        from src.sources import tracing_enabled

        if tracing_enabled():
            raise AcquisitionError("external_tracing_enabled")
        work = self.root / "tmp" / f"{gid}.check"
        try:
            files.make_private_directory(work)  # verified before the first byte is written
            path = work / "normalized_bundle.json"
            files._write(path, bundle)
            runtime = build_runtime(work / "clinical.sqlite", fixture=path, provider=None, enable_model=False)
            try:
                Generations._check_runtime(runtime, path, manifest["patient_id"])
                runtime.index.search(manifest["patient_id"], "record", k=1)
            finally:
                if not runtime.close():
                    raise AcquisitionError("runtime_not_drained")
        finally:
            files._remove(work)

    def approve(
        self,
        alias: str,
        attempt: str,
        candidate_hash: str,
        authorized: Callable[[], bool],
        fresh: Callable[[], bool],
    ) -> dict[str, Any]:
        """Promote one staged candidate. Every check precedes any change to the previous generation."""
        patient = self.allowlist.get(alias)
        if patient is None:
            raise RefreshRejected(404, "patient_unavailable")
        self._admit(alias, authorized)
        if not CANDIDATE_HASH.fullmatch(candidate_hash):
            raise RefreshRejected(400, "candidate_hash_required")  # malformed requests do not spend an elevation
        if not fresh():
            raise RefreshRejected(401, "step_up_required")  # one-shot step-up elevation (AUTH-1), consumed by this call
        try:
            row, manifest_bytes, bundle = self.staging.read_candidate(attempt, alias)
        except AcquisitionError as exc:
            raise _refusal(exc) from None
        if not secrets.compare_digest(row["manifest_sha256"], candidate_hash):
            raise RefreshRejected(409, "candidate_hash_mismatch")
        try:
            manifest = object_json(manifest_bytes)
            check_tier(manifest, alias, patient, bundle)
        except (ValueError, TypeError, KeyError):
            raise RefreshRejected(409, "candidate_not_approvable") from None
        if not manifest["synthetic_transport"] and not self.real_promotion:
            raise RefreshRejected(409, "real_promotion_disabled")
        gid = manifest["generation_id"]
        if self.catalog.db.execute(
            "SELECT 1 FROM live_generation WHERE generation_id = ? OR attempt_id = ?", (gid, attempt)
        ).fetchone():
            raise RefreshRejected(409, "generation_exists")
        try:
            files.verify_private_path(self.root)  # mandatory before any LIVE-labelled byte is written
            self._ingest_check(gid, manifest, bundle)
        except AcquisitionError as exc:
            raise _refusal(exc) from None
        except (OSError, ValueError, TypeError, KeyError, RuntimeError):
            raise RefreshRejected(409, "candidate_not_approvable") from None
        self._hook("after_check")
        temporary, final = self.root / "tmp" / f"{gid}.obj", self.root / "objects" / gid
        committed = False
        try:
            files.make_private_directory(temporary)  # verified before the first byte is written
            files._write(temporary / "manifest.json", manifest_bytes)
            files._write(temporary / "normalized_bundle.json", bundle)
            files._sync_directory(temporary)
            if (temporary / "manifest.json").read_bytes() != manifest_bytes or (
                temporary / "normalized_bundle.json"
            ).read_bytes() != bundle:
                raise RefreshRejected(409, "generation_integrity")
            files.verify_private_path(temporary)
            self._hook("after_tmp")
            os.replace(temporary, final)
            files._sync_directory(self.root / "objects")
            files.verify_private_path(final)
            self._hook("after_rename")
            # Last boundary: no await from here to the commit, so no revocation can interleave.
            self._admit(alias, authorized)
            self.staging.read_candidate(attempt, alias)
            epoch, revision = self._fence(attempt, alias)
            self._hook("before_commit")
            now = self.catalog.clock()
            with self.catalog.db:
                self.catalog.db.execute(
                    "UPDATE live_generation SET state = 'erase_pending', erase_reason = 'superseded' "
                    "WHERE alias = ? AND state = 'approved'",
                    (alias,),
                )
                self.catalog.db.execute(
                    "INSERT INTO live_generation (generation_id, alias, attempt_id, state, manifest_sha256, "
                    "retrieved_at, approved_at, expires_at) VALUES (?, ?, ?, 'approved', ?, ?, ?, ?)",
                    (
                        gid,
                        alias,
                        attempt,
                        row["manifest_sha256"],
                        manifest["retrieved_at"],
                        now,
                        now + GENERATION_RETENTION_S,
                    ),
                )
                moved = self.catalog.db.execute(
                    "UPDATE patient_head SET active = ?, last_success = ?, rolled_back = 0, epoch = epoch + 1 "
                    "WHERE alias = ? AND epoch = ? AND revision = ?",
                    (gid, gid, alias, epoch, revision),
                )
                if moved.rowcount != 1:
                    raise RefreshRejected(409, "publication_fenced")  # rolls the whole transaction back
                self.catalog._audit("live_generation_approved", attempt)
            committed = True
        except BaseException as exc:
            files._remove(temporary)
            if not committed:
                files._remove(final)
            if isinstance(exc, (RefreshRejected, AcquisitionError)) or not isinstance(exc, Exception):
                if isinstance(exc, AcquisitionError):
                    raise _refusal(exc) from None
                raise
            raise RefreshRejected(409, "generation_integrity") from None
        self._hook("after_commit")
        # The new generation is already authoritative; these are best-effort and retried by purge/sweep.
        self.staging.erase_attempt(attempt, "promoted")
        self.purge()
        return self.view(alias) or {}

    # -- erasure -------------------------------------------------------------------------------------------

    def _erase(self, gid: str, reason: str) -> bool:
        row = self.catalog.db.execute(
            "SELECT state, alias FROM live_generation WHERE generation_id = ?", (gid,)
        ).fetchone()
        if row is None or row["state"] == "erased":
            return True
        gone = GENERATION_ID.fullmatch(gid) is not None and all(
            files._remove(path)
            for path in (
                self.root / "objects" / gid,
                self.root / "tmp" / f"{gid}.obj",
                self.root / "tmp" / f"{gid}.check",
            )
        )
        now = self.catalog.clock()
        with self.catalog.db:
            if gone:
                self.catalog.db.execute(
                    "UPDATE live_generation SET state = 'erased', manifest_sha256 = NULL, erased_at = ?, "
                    "erase_reason = COALESCE(erase_reason, ?) WHERE generation_id = ?",
                    (now, reason[:48], gid),
                )
                self.catalog.db.execute(
                    "UPDATE patient_head SET active = NULL WHERE alias = ? AND active = ?", (row["alias"], gid)
                )
                self.catalog._audit("live_generation_erased")
            else:
                self.catalog.db.execute(
                    "UPDATE live_generation SET state = 'erase_pending' WHERE generation_id = ?", (gid,)
                )
                self.catalog._audit("live_erase_failed")
        return gone

    def _erase_where(self, clause: str, params: tuple[Any, ...], reason: str) -> int:
        rows = self.catalog.db.execute(
            f"SELECT generation_id FROM live_generation WHERE state != 'erased' AND {clause}", params
        ).fetchall()
        return sum(1 for row in rows if self._erase(row["generation_id"], reason))

    def erase_alias(self, alias: str, reason: str) -> int:
        return self._erase_where("alias = ?", (alias,), reason)

    def erase_all(self, reason: str) -> int:
        return self._erase_where("1 = 1", (), reason)

    def purge(self) -> int:
        """Erase expired (30 days), revoked, superseded and previously failed-to-erase generations."""
        now, count = self.catalog.clock(), 0
        owner_revoked = self.catalog.revoked("owner")
        for row in self.catalog.db.execute(
            "SELECT generation_id, alias, state, expires_at FROM live_generation WHERE state != 'erased'"
        ).fetchall():
            if owner_revoked:
                reason = "owner_revoked"
            elif self.catalog.revoked(row["alias"]):
                reason = "revoked"
            elif row["state"] == "erase_pending":
                reason = "retry"
            elif row["expires_at"] <= now:
                reason = "expired"
            else:
                continue
            count += self._erase(row["generation_id"], reason)
        return count

    def sweep(self) -> None:
        """Startup recovery: retention and revocation, then drop temporary and unowned files."""
        self.purge()
        approved = {
            row["generation_id"]
            for row in self.catalog.db.execute("SELECT generation_id FROM live_generation WHERE state = 'approved'")
        }
        for name in ("tmp", "objects"):
            with os.scandir(self.root / name) as entries:
                found = [(entry.name, Path(entry.path)) for entry in entries]
            for entry_name, path in found:
                if name == "tmp" or entry_name not in approved:
                    files._remove(path)
                    self.catalog.audit("live_orphan_erased")
        # A crash between the pointer commit and the candidate erase leaves a promoted candidate behind.
        for row in self.catalog.db.execute(
            "SELECT attempt_id FROM live_generation WHERE state = 'approved'"
        ).fetchall():
            self.staging.erase_attempt(row["attempt_id"], "promoted")
