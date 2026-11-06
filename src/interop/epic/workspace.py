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
    preferred_note_attachment,
    relative_reference,
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


# --------------------------------------------------------------------------- note coverage
def _source_reference(resource: dict[str, Any]) -> str | None:
    for extension in resource.get("extension") or []:
        if extension.get("url") == PROVENANCE_URL:
            for part in extension.get("extension") or []:
                if part.get("url") == "sourceReference":
                    return part.get("valueString")
    return None


def _encounter_reference(document: dict[str, Any]) -> str | None:
    encounters = (document.get("context") or {}).get("encounter") or []
    reference = (encounters[0] or {}).get("reference") if encounters else None
    return reference if isinstance(reference, str) else None


def summarize_note_coverage(
    raw_resources: list[dict[str, Any]],
    accepted: list[dict[str, Any]],
    fetched_references: set[str],
    fetch: Mapping[str, Any],
) -> dict[str, Any]:
    """Which Epic notes a snapshot holds, why the others are missing, and which are historical.

    Statuses: ``imported``; ``excluded_by_cap`` (eligible but beyond the note cap); ``body_unavailable``
    (no HTML/plain-text body, the fetch failed, or the fetched body was unusable); ``not_eligible``
    (entered in error, or no full clinical date); ``not_fetched_unattributed`` (an older snapshot that did
    not record its selection and had failed fetches). Counts, note types and clinical dates only.
    """
    from src.interop.epic import importer  # local import: importer imports this module

    documents = [r for r in raw_resources if r.get("resourceType") == "DocumentReference"]
    mapped = {ref: r for r in accepted if r.get("resourceType") == "DocumentReference" and (ref := _source_reference(r))}
    present = {f"{r['resourceType']}/{r['id']}" for r in accepted}
    selection = fetch.get("note_selection") or {}
    recorded = selection.get("policy") == importer.NOTE_SELECTION_POLICY and isinstance(selection.get("cap"), int)
    selected = {d["id"] for d in importer.select_notes(documents, selection["cap"]).selected} if recorded else None
    failed_fetches = int((fetch.get("notes") or {}).get("failed", 0))
    reference_date, periods = importer.note_periods(documents)

    statuses: Counter[str] = Counter()
    by_type: dict[str, Counter[str]] = {}
    reasons: dict[str, Counter[str]] = {}
    imported_rows: list[dict[str, Any]] = []
    missing_key_rows: list[dict[str, Any]] = []
    dates_preserved = encounters_preserved = True
    for document in documents:
        kind = importer.note_type(document)[0]
        source = f"DocumentReference/{document['id']}"
        attachment = preferred_note_attachment(document)
        binary = relative_reference(attachment["url"]) if attachment and isinstance(attachment.get("url"), str) else None
        ineligible = importer.note_ineligibility(document)
        reason = None
        if source in mapped:
            status = "imported"
        elif ineligible == "no_fetchable_attachment":
            status, reason = "body_unavailable", "no_html_or_plain_text_body"
        elif ineligible:
            status, reason = "not_eligible", ineligible
        elif binary in fetched_references:
            status, reason = "body_unavailable", "fetched_body_unusable"
        elif selected is not None:
            status, reason = ("body_unavailable", "fetch_failed") if document["id"] in selected else ("excluded_by_cap", None)
        elif failed_fetches == 0:
            status = "excluded_by_cap"
        else:
            status, reason = "not_fetched_unattributed", "cap_or_fetch_failure"
        statuses[status] += 1
        by_type.setdefault(status, Counter())[kind] += 1
        if reason:
            reasons.setdefault(status, Counter())[reason] += 1
        when = importer.note_clinical_time(document)
        row: dict[str, Any] = {
            "type": kind,
            "date": when.date().isoformat() if when else None,
            "period": periods.get(document["id"]),
        }
        if status == "imported":
            target = mapped[source]
            source_time, mapped_time = when, importer.note_clinical_time(target)
            row["date_preserved"] = source_time is not None and source_time == mapped_time
            source_encounter, mapped_encounter = _encounter_reference(document), _encounter_reference(target)
            row["encounter_linked"] = mapped_encounter is not None and mapped_encounter in present
            dates_preserved &= row["date_preserved"]
            if source_encounter is not None:
                encounters_preserved &= row["encounter_linked"]
            imported_rows.append(row)
        elif kind in importer.KEY_NOTE_TYPES:
            missing_key_rows.append({**row, "status": status})
    imported_types = {row["type"] for row in imported_rows}
    available_keys = [k for k in importer.KEY_NOTE_TYPES if any(importer.note_type(d)[0] == k for d in documents)]
    imported_rows.sort(key=lambda r: (r["date"] or "", r["type"]))
    return {
        "policy": selection.get("policy") if recorded else None,
        "cap": selection.get("cap") if recorded else None,
        "available": len(documents),
        "by_status": dict(sorted(statuses.items())),
        "by_status_and_type": {s: importer.ordered_note_counts(c) for s, c in sorted(by_type.items())},
        "reasons": {s: dict(sorted(c.items())) for s, c in sorted(reasons.items())},
        "key_types_not_imported": [k for k in available_keys if k not in imported_types],
        "historical_reference_date": reference_date,
        "historical_after_days": importer.HISTORICAL_AFTER_DAYS,
        "imported_notes": imported_rows,
        "key_notes_not_imported": missing_key_rows,
        "original_dates_preserved": dates_preserved,
        "encounter_links_preserved": encounters_preserved,
    }


def note_coverage(snapshot: Snapshot | str | Path) -> dict[str, Any]:
    """Note coverage of a stored snapshot, computed offline from its raw records, Binary index and bundle."""
    snap = snapshot if isinstance(snapshot, Snapshot) else open_snapshot(snapshot)
    raw = json.loads((snap.path / "raw" / "resources.json").read_text(encoding="utf-8"))
    index = json.loads((snap.path / "raw" / "binaries.json").read_text(encoding="utf-8"))
    bundle = json.loads(snap.bundle_path.read_text(encoding="utf-8"))
    accepted = [entry["resource"] for entry in bundle.get("entry") or []]
    return summarize_note_coverage(raw, accepted, set(index), snap.manifest.get("fetch") or {})


def note_coverage_sentence(coverage: Mapping[str, Any]) -> str:
    """One plain sentence for scope notes: imported, excluded by the cap, unavailable bodies, historical notes."""
    from src.interop.epic.importer import NOTE_TYPE_LABELS

    def by_type(status: str) -> str:
        counts = coverage["by_status_and_type"].get(status) or {}
        return ", ".join(f"{NOTE_TYPE_LABELS.get(k, k)} {n}" for k, n in counts.items())

    available, statuses = coverage["available"], coverage["by_status"]
    if not available:
        return "Clinical notes: none were returned by Epic for this patient."
    imported = statuses.get("imported", 0)
    policy = f"policy {coverage['policy']}, cap {coverage['cap']}" if coverage.get("policy") else "selection policy not recorded"
    parts = [f"Clinical notes: {imported} of {available} imported ({policy})"]
    for status, text in (
        ("excluded_by_cap", "excluded by the import cap"),
        ("body_unavailable", "no usable note body"),
        ("not_eligible", "not eligible"),
        ("not_fetched_unattributed", "not fetched (cap or fetch failure)"),
    ):
        if statuses.get(status):
            parts.append(f"{statuses[status]} {text} ({by_type(status)})")
    sentence = "; ".join(parts) + "."
    historical = [r for r in coverage["imported_notes"] if r.get("period") == "historical"]
    if historical:
        listed = ", ".join(f"{NOTE_TYPE_LABELS.get(r['type'], r['type'])} {r['date']}" for r in historical)
        sentence += (
            f" Historical notes (dated more than {coverage['historical_after_days']} days before the newest note,"
            f" {coverage['historical_reference_date']}): {listed}."
        )
    if coverage["key_types_not_imported"]:
        missing = ", ".join(NOTE_TYPE_LABELS.get(k, k) for k in coverage["key_types_not_imported"])
        sentence += f" Key note types present in Epic but not imported: {missing}."
    return sentence + " Answers cover imported notes only; each note keeps its original clinical date."


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
            note_coverage_sentence(
                summarize_note_coverage(raw_resources, accepted, set(binaries), fetch_summary or {})
            ),
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


def medication_coverage(manifest: Mapping[str, Any]) -> dict[str, str]:
    """What the snapshot's own manifest says about structured medication orders.

    "not_imported": medication import was off, or the search failed or was not recorded.
    "returned_none": the search completed and Epic returned no MedicationRequest.
    Empty: medication orders were imported.
    """
    if not manifest.get("include_medications"):
        return {"MedicationRequest": "not_imported"}
    operations = (manifest.get("fetch") or {}).get("operations") or []
    searched = any(op.get("operation") == "MedicationRequest.Search" and op.get("status") == "ok"
                   for op in operations)
    if not searched:
        return {"MedicationRequest": "not_imported"}
    if not (manifest.get("resource_counts") or {}).get("MedicationRequest"):
        return {"MedicationRequest": "returned_none"}
    return {}


def load_runtime(path: str | Path, *, provider: Any = None) -> Runtime:
    """Build the unchanged application runtime over one snapshot's own database (model disabled)."""
    from src.runtime import build_runtime

    snapshot = open_snapshot(path)
    database = require_workspace_path(snapshot.db_path)
    runtime = build_runtime(database, fixture=snapshot.bundle_path, provider=provider, enable_model=False)
    runtime.records.source_coverage = medication_coverage(snapshot.manifest)
    return runtime


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
