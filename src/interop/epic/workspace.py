"""Isolated, immutable Epic sandbox snapshots and the runtime that reads them (local only).

A snapshot directory ``var/epic_sandbox/<snapshot_id>/`` holds:

* ``manifest.json``: snapshot id, source, mapping version, imported internal patient
  ids, counts, hashes, fetch summary and MOCK/LIVE label (no clinical content);
* ``normalized_bundle.json``: the mapped, contract-conformant resources;
* ``epic.db``: a separate SQLite database built by the unchanged ``build_runtime``;
* ``raw/``: the raw Epic resources and note bytes, kept only so mapping can be re-audited;
* ``reports/``: sanitised reports.

Snapshots are never modified after creation and never touch the default synthetic
runtime database. ``load_runtime`` is the single entry point a future, separately
approved local source switch (M2) would call.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Mapping

from src.clinical.store import FHIRStore
from src.interop.epic import settings
from src.interop.epic.client import BinaryContent
from src.interop.epic.mapping import (
    MAPPED_TYPES,
    MAPPING_VERSION,
    PROVENANCE_URL,
    EpicMapper,
    MappingReport,
    content_sha256,
)
from src.persistence.db import DEFAULT_DB_PATH, Database
from src.safety.policy import AccessContext, Persona

if TYPE_CHECKING:  # pragma: no cover
    from src.runtime import Runtime

MANIFEST_VERSION = "epic-sandbox-workspace/1"
# A1 (medication dates) needs separate owner approval: until then MedicationRequest is not imported,
# because existing medication views treat meta.lastUpdated as a medication date.
MEDICATION_IMPORT_ENABLED = False
MEDICATION_SKIP_REASON = (
    "not imported: existing medication views treat meta.lastUpdated as a medication date; "
    "import waits for the separately approved A1 fix"
)
_SNAPSHOT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{7,63}$")
# Internal references: Epic-style ids plus the ``synthetic_epic...`` patient id (underscore allowed).
_REF = re.compile(r"^([A-Z][A-Za-z]+)/([A-Za-z0-9_.\-]{1,256})$")


class WorkspaceError(RuntimeError):
    """The isolated workspace refused an operation. Messages hold no clinical content."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def new_snapshot_id() -> str:
    return f"{datetime.now(timezone.utc):%Y%m%dt%H%M%S}-{uuid.uuid4().hex[:8]}"


def require_workspace_path(path: str | Path) -> Path:
    """Allow only locations strictly inside ``var/epic_sandbox/``; never the default runtime database."""
    resolved = Path(path).expanduser().resolve()
    root = settings.workspace_root().resolve()
    if root not in resolved.parents:
        raise WorkspaceError("Epic sandbox data may only be stored under var/epic_sandbox/")
    protected = {DEFAULT_DB_PATH.resolve()}
    configured = os.environ.get("CLINICAL_TRACE_DB_PATH")
    if configured and configured != ":memory:":
        protected.add(Path(configured).expanduser().resolve())
    if resolved in protected:
        raise WorkspaceError("refusing to use the application's runtime database for Epic sandbox data")
    return resolved


def resolve_snapshot(name_or_path: str | Path) -> Path:
    text = str(name_or_path)
    if _SNAPSHOT_ID.fullmatch(text):
        return settings.workspace_root() / text
    return Path(text)


@dataclass(frozen=True)
class Snapshot:
    path: Path
    manifest: dict[str, Any]

    @property
    def db_path(self) -> Path:
        return self.path / "epic.db"

    @property
    def bundle_path(self) -> Path:
        return self.path / "normalized_bundle.json"

    @property
    def patient_ids(self) -> tuple[str, ...]:
        return tuple(p["internal_id"] for p in self.manifest["patients"])


def open_snapshot(path: str | Path) -> Snapshot:
    directory = require_workspace_path(resolve_snapshot(path))
    try:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise WorkspaceError("not an Epic sandbox snapshot (manifest missing or unreadable)") from None
    if manifest.get("manifest_version") != MANIFEST_VERSION:
        raise WorkspaceError("unsupported snapshot manifest version")
    snapshot = Snapshot(directory, manifest)
    if content_sha256(snapshot.bundle_path.read_bytes()) != manifest.get("bundle_sha256"):
        raise WorkspaceError("the normalised bundle no longer matches its manifest; snapshots are immutable")
    return snapshot


def count_dangling_references(resources: Iterable[dict[str, Any]]) -> int:
    resources = list(resources)
    present = {f"{r['resourceType']}/{r['id']}" for r in resources}
    dangling = 0

    def walk(value: Any) -> None:
        nonlocal dangling
        if isinstance(value, dict):
            reference = value.get("reference")
            if isinstance(reference, str):
                match = _REF.fullmatch(reference)
                if match and match.group(1) in MAPPED_TYPES and reference not in present:
                    dangling += 1
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    for resource in resources:
        walk({k: v for k, v in resource.items() if k not in ("meta", "extension")})
    return dangling


def map_resources(
    source_patient_id: str,
    raw_resources: Iterable[dict[str, Any]],
    binaries: Mapping[str, BinaryContent],
    *,
    snapshot_id: str,
    retrieved_at: str,
    include_medications: bool = False,
) -> tuple[EpicMapper, list[dict[str, Any]], MappingReport]:
    """Deterministically map one patient's raw resources. Raises on any cross-patient resource."""
    mapper = EpicMapper(source_patient_id, snapshot_id=snapshot_id, retrieved_at=retrieved_at)
    report = MappingReport()
    accepted: list[dict[str, Any]] = []
    for raw in raw_resources:
        if raw.get("resourceType") == "MedicationRequest" and not include_medications:
            report.received["MedicationRequest"] += 1
            report.skipped_types["MedicationRequest"] = MEDICATION_SKIP_REASON
            continue
        outcome = mapper.map(raw, binaries)
        report.add(outcome)
        if outcome.resource is not None:
            accepted.append(outcome.resource)
    report.dangling_references = count_dangling_references(accepted)
    return mapper, accepted, report


def _bundle_bytes(resources: list[dict[str, Any]]) -> bytes:
    ordered = sorted(resources, key=lambda r: (MAPPED_TYPES.index(r["resourceType"]), r["id"]))
    bundle = {"resourceType": "Bundle", "type": "collection", "entry": [{"resource": r} for r in ordered]}
    return (json.dumps(bundle, indent=1, sort_keys=True) + "\n").encode("utf-8")


def write_snapshot(
    source_patient_id: str,
    raw_resources: list[dict[str, Any]],
    binaries: Mapping[str, BinaryContent],
    *,
    retrieved_at: str,
    mode: str,
    snapshot_id: str | None = None,
    include_medications: bool = False,
    fetch_summary: dict[str, Any] | None = None,
) -> Snapshot:
    """Map, validate and store one patient as a new immutable snapshot, then build its isolated database."""
    if mode not in ("LIVE", "MOCK"):
        raise ValueError("mode must be LIVE or MOCK")
    if include_medications and not MEDICATION_IMPORT_ENABLED:
        raise WorkspaceError(MEDICATION_SKIP_REASON)
    snapshot_id = snapshot_id or new_snapshot_id()
    if not _SNAPSHOT_ID.fullmatch(snapshot_id):
        raise WorkspaceError("invalid snapshot id")
    directory = require_workspace_path(settings.workspace_root() / snapshot_id)
    mapper, accepted, report = map_resources(
        source_patient_id,
        raw_resources,
        binaries,
        snapshot_id=snapshot_id,
        retrieved_at=retrieved_at,
        include_medications=include_medications,
    )
    if not any(r["resourceType"] == "Patient" for r in accepted):
        raise WorkspaceError("the Patient resource could not be mapped; nothing was written")
    bundle = _bundle_bytes(accepted)
    try:
        directory.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise WorkspaceError("snapshot already exists; snapshots are immutable") from None
    raw_dir = directory / "raw"
    (raw_dir / "binaries").mkdir(parents=True)
    (directory / "reports").mkdir()
    (raw_dir / "resources.json").write_text(json.dumps(raw_resources, indent=1, sort_keys=True), encoding="utf-8")
    index = {}
    for reference, binary in sorted(binaries.items()):
        digest = content_sha256(binary.data)
        (raw_dir / "binaries" / f"{digest[:32]}.bin").write_bytes(binary.data)
        index[reference] = {"content_type": binary.content_type, "sha256": digest, "file": f"{digest[:32]}.bin"}
    (raw_dir / "binaries.json").write_text(json.dumps(index, indent=1, sort_keys=True), encoding="utf-8")
    (directory / "normalized_bundle.json").write_bytes(bundle)
    manifest: dict[str, Any] = {
        "manifest_version": MANIFEST_VERSION,
        "snapshot_id": snapshot_id,
        "mode": mode,
        "source": settings.SOURCE_SYSTEM,
        "fhir_base": settings.FHIR_BASE,
        "mapping_version": MAPPING_VERSION,
        "created_at": utc_now(),
        "retrieved_at": retrieved_at,
        "include_medications": include_medications,
        "patients": [{"internal_id": mapper.patient_id, "source_patient_id": source_patient_id}],
        "resource_counts": dict(sorted(Counter(r["resourceType"] for r in accepted).items())),
        "mapping": report.as_dict(),
        "fetch": fetch_summary or {},
        "bundle_sha256": content_sha256(bundle),
        "notes": [
            "Readmission model disabled for this source (enable_model=False).",
            *([f"MedicationRequest {MEDICATION_SKIP_REASON}."] if not include_medications else []),
        ],
    }
    _write_manifest(directory, manifest)
    runtime = load_runtime(directory)
    try:
        stored_patients = runtime.store.patient_ids()
        ingestion = {k: v for k, v in runtime.startup_ingestion.items() if k in ("received", "inserted", "invalid")}
    finally:
        runtime.close()
    if stored_patients != [mapper.patient_id]:
        raise WorkspaceError("isolation check failed: the snapshot database holds unexpected patients")
    manifest["ingestion"] = ingestion
    _write_manifest(directory, manifest)
    return Snapshot(directory, manifest)


def _write_manifest(directory: Path, manifest: dict[str, Any]) -> None:
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_runtime(path: str | Path, *, provider: Any = None) -> Runtime:
    """Build the unchanged application runtime over one snapshot's own database (model disabled)."""
    from src.runtime import build_runtime

    snapshot = open_snapshot(path)
    database = require_workspace_path(snapshot.db_path)
    return build_runtime(database, fixture=snapshot.bundle_path, provider=provider, enable_model=False)


def local_access(snapshot: Snapshot, role: str = "clinician") -> AccessContext:
    """An in-memory persona whose panel is exactly this snapshot's patients (not an authentication mechanism)."""
    persona = Persona(
        persona_id=f"epic_sandbox_{role}",
        display_name=f"Epic sandbox {role} (local CLI)",
        role=role,
        panel=snapshot.patient_ids,
        description="Local command-line persona scoped to one Epic sandbox snapshot.",
    )
    return AccessContext(persona=persona, request_id=str(uuid.uuid4()))


def citation_provenance(runtime: Runtime, result: dict[str, Any]) -> dict[str, int]:
    """Resolve each cited evidence item back to a stored resource carrying Epic provenance."""
    evidence = {item["evidence_id"]: item for item in result.get("evidence") or []}
    cited = {c for claim in (result.get("answer") or {}).get("claims") or [] for c in claim.get("citations") or []}
    counts = Counter({"citations": len(cited)})
    for evidence_id in cited:
        item = evidence.get(evidence_id)
        reference = (item or {}).get("source_ref") or ""
        match = _REF.fullmatch(reference)
        resource = runtime.store.get(match.group(1), match.group(2)) if match else None
        if resource is None:
            counts["unresolved"] += 1
            continue
        has_extension = any(e.get("url") == PROVENANCE_URL for e in resource.get("extension") or [])
        if str((resource.get("meta") or {}).get("source", "")).startswith(settings.FHIR_BASE + "/") and has_extension:
            counts["resolved_to_epic_source"] += 1
        else:
            counts["resolved_without_epic_provenance"] += 1
    return dict(counts)


def review_summary(result: dict[str, Any]) -> dict[str, Any]:
    """Content-free summary of an agent run: counts, statuses and source types only."""
    answer = result.get("answer") or {}
    evidence = result.get("evidence") or []
    cited_types = Counter(str(item.get("source_ref", "")).split("/", 1)[0] for item in evidence if item.get("cited"))
    return {
        "outcome": result.get("outcome"),
        "refusal_category": (result.get("refusal") or {}).get("category"),
        "verification": result.get("verification"),
        "claims_accepted": len(answer.get("claims") or []),
        "claims_rejected": len(answer.get("rejected_claims") or []),
        "cited_source_types": dict(sorted(cited_types.items())),
        "tool_calls": [
            {k: call.get(k) for k in ("tool", "ok", "error_type", "evidence_count")}
            for call in result.get("tool_calls") or []
        ],
        "risk_available": bool((result.get("risk") or {}).get("available")),
        "review_required": (result.get("review") or {}).get("required"),
        "inference": (result.get("inference") or {}).get("label"),
        "error": result.get("error"),
    }


def verify_snapshot(path: str | Path) -> dict[str, Any]:
    """Re-map the raw snapshot and check the bundle, the database and every provenance hash."""
    snapshot = open_snapshot(path)
    raw_dir = snapshot.path / "raw"
    raw_resources = json.loads((raw_dir / "resources.json").read_text(encoding="utf-8"))
    index = json.loads((raw_dir / "binaries.json").read_text(encoding="utf-8"))
    binaries: dict[str, BinaryContent] = {}
    binary_mismatches = 0
    for reference, entry in index.items():
        data = (raw_dir / "binaries" / entry["file"]).read_bytes()
        if content_sha256(data) != entry["sha256"]:
            binary_mismatches += 1
        binaries[reference] = BinaryContent(reference=reference, content_type=entry["content_type"], data=data)
    manifest = snapshot.manifest
    _, accepted, _ = map_resources(
        manifest["patients"][0]["source_patient_id"],
        raw_resources,
        binaries,
        snapshot_id=manifest["snapshot_id"],
        retrieved_at=manifest["retrieved_at"],
        include_medications=bool(manifest.get("include_medications")),
    )
    bundle_matches = _bundle_bytes(accepted) == snapshot.bundle_path.read_bytes()
    raw_hashes = {f"{r.get('resourceType')}/{r.get('id')}": content_sha256(r) for r in raw_resources}
    store_mismatches = hash_mismatches = 0
    database = Database(require_workspace_path(snapshot.db_path))
    try:
        store = FHIRStore(database)
        for resource in accepted:
            if store.get(resource["resourceType"], resource["id"]) != resource:
                store_mismatches += 1
            parts = next(e for e in resource["extension"] if e["url"] == PROVENANCE_URL)["extension"]
            values = {p["url"]: next(v for k, v in p.items() if k.startswith("value")) for p in parts}
            if raw_hashes.get(values["sourceReference"]) != values["sourceContentSha256"]:
                hash_mismatches += 1
        stored_patients = store.patient_ids()
    finally:
        database.close()
    ok = (
        bundle_matches
        and not store_mismatches
        and not hash_mismatches
        and not binary_mismatches
        and stored_patients == list(snapshot.patient_ids)
    )
    return {
        "ok": ok,
        "snapshot_id": manifest["snapshot_id"],
        "mode": manifest["mode"],
        "resources_checked": len(accepted),
        "bundle_matches_remap": bundle_matches,
        "store_mismatches": store_mismatches,
        "provenance_hash_mismatches": hash_mismatches,
        "binary_hash_mismatches": binary_mismatches,
        "patients_isolated": stored_patients == list(snapshot.patient_ids),
    }
