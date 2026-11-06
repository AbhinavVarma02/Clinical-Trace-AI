"""Bounded LIVE retrieval of one Epic sandbox test patient (used only after owner approval).

``smoke`` performs one read and one search per enabled (type, category) and reports
shapes and counts only. ``fetch_patient`` additionally fetches note content for at
most ``max_notes`` DocumentReferences, only from Binary references those verified
documents cite. Neither function loops across patients or stores anything; the
workspace module decides what is persisted.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from src.interop.epic.client import (
    CORE_TYPES,
    SEARCH_PLAN,
    BinaryContent,
    EpicFHIRError,
    EpicSandboxClient,
)
from src.interop.epic.mapping import attachment_kind, preferred_note_attachment, relative_reference
from src.interop.epic.telemetry import safe_code
from src.interop.epic.workspace import MEDICATION_IMPORT_ENABLED, MEDICATION_SKIP_REASON


@dataclass
class FetchResult:
    raw_resources: list[dict[str, Any]] = field(default_factory=list)
    binaries: dict[str, BinaryContent] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)


def _plan(types: tuple[str, ...]) -> list[tuple[str, str | None]]:
    return [(rtype, category) for rtype in types for category in SEARCH_PLAN[rtype]]


def _shape(resources: list[dict[str, Any]]) -> dict[str, Any]:
    """Structural observations only (field presence, systems, formats): no values or text."""
    shape: Counter[str] = Counter()
    for resource in resources:
        meta: dict[str, Any] = resource["meta"] if isinstance(resource.get("meta"), dict) else {}
        shape["meta.lastUpdated present"] += bool(meta.get("lastUpdated"))
        shape["meta.versionId present"] += bool(meta.get("versionId"))
        for key in (
            "effectiveDateTime",
            "effectivePeriod",
            "effectiveInstant",
            "medicationReference",
            "medicationCodeableConcept",
            "component",
            "valueQuantity",
            "valueCodeableConcept",
        ):
            if key in resource:
                shape[f"has {key}"] += 1
        klass = resource.get("class")
        if isinstance(klass, dict):
            shape[f"class.system {safe_code(klass.get('system', 'none').rsplit('/', 1)[-1])}"] += 1
        for item in resource.get("content") or []:
            attachment = item.get("attachment") if isinstance(item, dict) else None
            if isinstance(attachment, dict):
                shape[f"attachment {safe_code(attachment_kind(attachment) or 'none')}"] += 1
                shape["attachment inline data" if "data" in attachment else "attachment url"] += 1
    return dict(sorted(shape.items()))


def smoke(client: EpicSandboxClient, patient_id: str, *, include_all_types: bool = False) -> dict[str, Any]:
    """Patient.Read plus one search per enabled (type, category); counts and shapes only."""
    types = tuple(SEARCH_PLAN) if include_all_types else CORE_TYPES
    patient = client.read_patient(patient_id)
    operations: list[dict[str, Any]] = [{"operation": "Patient.Read", "status": "ok", "shape": _shape([patient])}]
    for rtype, category in _plan(types):
        name = f"{rtype}.Search" + (f" ({category})" if category else "")
        try:
            result = client.search(rtype, patient_id, category=category)
        except EpicFHIRError as exc:
            operations.append({"operation": name, "status": "failed", "error": exc.category})
            continue
        operations.append({"operation": name, "status": "ok", **result.summary(), "shape": _shape(result.resources)})
    return {"operations": operations, "all_ok": all(op["status"] == "ok" for op in operations)}


def fetch_patient(
    client: EpicSandboxClient, patient_id: str, *, include_medications: bool = False, max_notes: int = 10
) -> FetchResult:
    """Retrieve one patient for import. Patient-scope violations propagate and abort the import."""
    if include_medications and not MEDICATION_IMPORT_ENABLED:
        raise ValueError(MEDICATION_SKIP_REASON)
    if not 0 <= max_notes <= 25:
        raise ValueError("max_notes must be between 0 and 25")
    fetched = FetchResult()
    patient = client.read_patient(patient_id)
    fetched.raw_resources.append(patient)
    seen = {("Patient", patient_id)}
    operations: list[dict[str, Any]] = [{"operation": "Patient.Read", "status": "ok"}]
    for rtype, category in _plan(tuple(SEARCH_PLAN)):
        name = f"{rtype}.Search" + (f" ({category})" if category else "")
        if rtype == "MedicationRequest" and not include_medications:
            operations.append({"operation": name, "status": "skipped", "reason": "A1 pending"})
            continue
        try:
            result = client.search(rtype, patient_id, category=category)
        except EpicFHIRError as exc:
            operations.append({"operation": name, "status": "failed", "error": exc.category})
            continue
        operations.append({"operation": name, "status": "ok", **result.summary()})
        for resource in result.resources:
            key = (resource["resourceType"], resource["id"])
            if key not in seen:
                seen.add(key)
                fetched.raw_resources.append(resource)

    documents = sorted(
        (r for r in fetched.raw_resources if r["resourceType"] == "DocumentReference"),
        key=lambda d: str(d.get("date") or ""),
        reverse=True,
    )
    cited: dict[str, str] = {}
    for document in documents:
        attachment = preferred_note_attachment(document)
        if attachment and isinstance(attachment.get("url"), str):
            cited[document["id"]] = relative_reference(attachment["url"])
    allowed = set(cited.values())
    notes: Counter[str] = Counter()
    for document in documents:
        reference = cited.get(document["id"])
        if reference is None:
            notes["no_fetchable_attachment"] += 1
            continue
        if notes["fetched"] + notes["failed"] >= max_notes:
            notes["skipped_note_cap"] += 1
            continue
        try:
            fetched.binaries[reference] = client.read_binary(reference, allowed=allowed)
            notes["fetched"] += 1
        except EpicFHIRError as exc:
            notes["failed"] += 1
            notes[f"failed.{exc.category}"] += 1
    fetched.summary = {
        "operations": operations,
        "notes": dict(sorted(notes.items())),
        "complete": all(op["status"] == "ok" for op in operations if op["status"] != "skipped"),
        "telemetry": client.telemetry.describe(),
    }
    return fetched
