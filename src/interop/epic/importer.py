"""Bounded LIVE retrieval of one Epic sandbox test patient (used only after owner approval).

``smoke`` performs one read and one search per enabled (type, category) and reports
shapes and counts only. ``fetch_patient`` additionally fetches note content for at
most ``max_notes`` DocumentReferences, only from Binary references those verified
documents cite. Which notes are fetched follows ``select_notes`` (policy
``epic-note-select/1``): the best Discharge summary, History and physical and Consult
note first, then the newest others. Neither function loops across patients or stores anything; the
workspace module decides what is persisted.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable

from src.clinical.fhir_models import parse_timestamp

from src.interop.epic.client import (
    CORE_TYPES,
    SEARCH_PLAN,
    BinaryContent,
    EpicFHIRError,
    EpicSandboxClient,
)
from src.interop.epic.mapping import LOINC_SYSTEM, attachment_kind, preferred_note_attachment, relative_reference
from src.interop.epic.telemetry import safe_code
from src.interop.epic.workspace import MEDICATION_IMPORT_ENABLED, MEDICATION_SKIP_REASON

# --------------------------------------------------------------------------- note selection
NOTE_SELECTION_POLICY = "epic-note-select/1"
# Clinically key note types in priority order. The best note of each is reserved before other notes.
KEY_NOTE_TYPES = ("discharge_summary", "history_and_physical", "consult")
NOTE_TYPE_LABELS = {
    "discharge_summary": "Discharge summary",
    "history_and_physical": "History and physical",
    "consult": "Consult",
    "progress": "Progress note",
    "procedure": "Procedure note",
    "other": "Other",
}
_NOTE_TYPE_RANK = {name: rank for rank, name in enumerate(NOTE_TYPE_LABELS)}
_LOINC_NOTE_TYPES = {
    "18842-5": "discharge_summary",
    "34117-2": "history_and_physical",
    "11488-4": "consult",
    "11506-3": "progress",
    "28570-0": "procedure",
}
_TEXT_NOTE_TYPES = (
    ("discharge_summary", re.compile(r"^discharge summar(?:y|ies)(?: note)?$")),
    ("history_and_physical", re.compile(r"^(?:h ?& ?p|history (?:and|&) physical(?: note)?)$")),
    ("consult", re.compile(r"^consult(?:s|ation)?(?: notes?)?$")),
    ("progress", re.compile(r"^progress notes?$")),
    ("procedure", re.compile(r"^procedures?(?: notes?)?$")),
)
_FINAL_DOC_STATUS = frozenset({"final", "amended"})
# A note dated more than this many days before the newest dated note is reported as historical.
HISTORICAL_AFTER_DAYS = 365


def ordered_note_counts(counts: Counter[str]) -> dict[str, int]:
    """Note-type counts in clinical priority order (key types first), zero counts dropped."""
    return {k: counts[k] for k in sorted(counts, key=lambda k: _NOTE_TYPE_RANK.get(k, len(_NOTE_TYPE_RANK))) if counts[k]}


def _text_note_type(label: str) -> str | None:
    normalized = " ".join(re.sub(r"[^a-z&]+", " ", label.lower()).split())
    return next((name for name, pattern in _TEXT_NOTE_TYPES if pattern.match(normalized)), None)


def note_type(document: dict[str, Any]) -> tuple[str, bool]:
    """(note type, conflict): LOINC first, then type text and coding displays; "other" if neither is known."""
    concept: dict[str, Any] = document["type"] if isinstance(document.get("type"), dict) else {}
    codings = [c for c in concept.get("coding") or [] if isinstance(c, dict)]
    loinc = [_LOINC_NOTE_TYPES[str(c.get("code"))] for c in codings
             if c.get("system") == LOINC_SYSTEM and str(c.get("code")) in _LOINC_NOTE_TYPES]
    labels = [concept.get("text"), *(c.get("display") for c in codings)]
    texts = [t for t in (_text_note_type(label) for label in labels if isinstance(label, str)) if t]
    candidates = loinc or texts
    if not candidates:
        return "other", False
    chosen = min(candidates, key=_NOTE_TYPE_RANK.__getitem__)
    return chosen, any(t != chosen for t in [*loinc, *texts])


def note_clinical_time(document: dict[str, Any]) -> datetime | None:
    """The note's own clinical time (``date``) as aware UTC, or None when it is missing or unparseable."""
    value = document.get("date")
    if not isinstance(value, str):
        return None
    try:
        return parse_timestamp(value)
    except ValueError:
        return None


def note_ineligibility(document: dict[str, Any]) -> str | None:
    """Why a note cannot be imported (no request is spent on it), or None when it is eligible."""
    if document.get("status") == "entered-in-error":
        return "entered_in_error"
    attachment = preferred_note_attachment(document)
    if attachment is None or not isinstance(attachment.get("url"), str):
        return "no_fetchable_attachment"
    if note_clinical_time(document) is None:
        return "no_full_clinical_time"
    return None


def note_periods(documents: Iterable[dict[str, Any]]) -> tuple[str | None, dict[str, str]]:
    """Reference date (newest dated note, a clinical date) and "historical"/"recent" for each dated note id.

    Retrieval time is never used: the reference is the newest clinical note date in the patient's own record.
    """
    dated = {
        d["id"]: t
        for d in documents
        if d.get("status") != "entered-in-error" and (t := note_clinical_time(d)) is not None
    }
    if not dated:
        return None, {}
    newest = max(dated.values())
    cutoff = newest - timedelta(days=HISTORICAL_AFTER_DAYS)
    periods = {doc_id: ("historical" if when < cutoff else "recent") for doc_id, when in dated.items()}
    return newest.date().isoformat(), periods


@dataclass
class NoteSelection:
    selected: list[dict[str, Any]]
    summary: dict[str, Any]


def select_notes(documents: list[dict[str, Any]], max_notes: int) -> NoteSelection:
    """Deterministic, bounded choice of the notes whose bodies are fetched (policy ``epic-note-select/1``).

    1. Eligible: not entered-in-error, an HTML or plain-text attachment by URL, and a full clinical date.
    2. The best note of each key type (Discharge summary, History and physical, Consult, in that order) is
       reserved: current and final/amended first, then newest, then lowest Epic id.
    3. Remaining slots go to the newest other eligible notes (ties: type priority, then Epic id).
    The input order never changes the result, and at most ``max_notes`` notes are chosen.
    """
    not_eligible: Counter[str] = Counter()
    eligible: list[dict[str, Any]] = []
    types: dict[str, str] = {}
    conflicts = 0
    for document in documents:
        types[document["id"]], conflict = note_type(document)
        conflicts += conflict
        reason = note_ineligibility(document)
        if reason:
            not_eligible[reason] += 1
        else:
            eligible.append(document)

    def recency(document: dict[str, Any]) -> float:
        when = note_clinical_time(document)
        return -when.timestamp() if when is not None else float("inf")  # eligible notes always have a time

    def preference(document: dict[str, Any]) -> tuple[int, float, str]:
        settled = document.get("status") in (None, "current") and document.get("docStatus") in _FINAL_DOC_STATUS
        return (0 if settled else 1, recency(document), document["id"])

    chosen: list[dict[str, Any]] = []
    for key in KEY_NOTE_TYPES:
        candidates = sorted((d for d in eligible if types[d["id"]] == key), key=preference)
        if candidates and len(chosen) < max_notes:
            chosen.append(candidates[0])
    chosen_ids = {d["id"] for d in chosen}
    rest = sorted(
        (d for d in eligible if d["id"] not in chosen_ids),
        key=lambda d: (recency(d), _NOTE_TYPE_RANK[types[d["id"]]], d["id"]),
    )
    chosen.extend(rest[: max(0, max_notes - len(chosen))])
    chosen_ids = {d["id"] for d in chosen}

    reference_date, periods = note_periods(documents)
    available_keys = [k for k in KEY_NOTE_TYPES if any(types[d["id"]] == k for d in eligible)]
    selected_keys = {types[d["id"]] for d in chosen}
    summary = {
        "policy": NOTE_SELECTION_POLICY,
        "cap": max_notes,
        "available": len(documents),
        "eligible": len(eligible),
        "not_eligible": dict(sorted(not_eligible.items())),
        "selected": len(chosen),
        "selected_by_type": ordered_note_counts(Counter(types[d["id"]] for d in chosen)),
        "excluded_by_cap_by_type": ordered_note_counts(
            Counter(types[d["id"]] for d in eligible if d["id"] not in chosen_ids)
        ),
        "selected_by_period": dict(sorted(Counter(periods[d["id"]] for d in chosen).items())),
        "historical_reference_date": reference_date,
        "historical_after_days": HISTORICAL_AFTER_DAYS,
        "selected_not_final": sum(1 for d in chosen if d.get("docStatus") not in _FINAL_DOC_STATUS),
        "type_conflicts": conflicts,
        "key_types_available": available_keys,
        "key_types_excluded_by_cap": [k for k in available_keys if k not in selected_keys],
    }
    return NoteSelection(selected=chosen, summary=summary)


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

    documents = [r for r in fetched.raw_resources if r["resourceType"] == "DocumentReference"]
    cited: dict[str, str] = {}
    for document in documents:
        attachment = preferred_note_attachment(document)
        if attachment and isinstance(attachment.get("url"), str):
            cited[document["id"]] = relative_reference(attachment["url"])
    allowed = set(cited.values())
    selection = select_notes(documents, max_notes)
    notes: Counter[str] = Counter()
    for reason, count in selection.summary["not_eligible"].items():
        notes[reason if reason == "no_fetchable_attachment" else f"not_eligible.{reason}"] += count
    notes["skipped_note_cap"] += sum(selection.summary["excluded_by_cap_by_type"].values())
    imported_types: Counter[str] = Counter()
    for document in selection.selected:  # at most max_notes Binary reads
        reference = cited[document["id"]]
        if reference not in fetched.binaries:
            try:
                fetched.binaries[reference] = client.read_binary(reference, allowed=allowed)
            except EpicFHIRError as exc:
                notes["failed"] += 1
                notes[f"failed.{exc.category}"] += 1
                continue
        else:
            notes["fetched_shared_binary"] += 1  # another selected note cites the same Binary: no new request
        notes["fetched"] += 1
        imported_types[note_type(document)[0]] += 1
    selection.summary["key_types_not_fetched"] = [
        key for key in selection.summary["key_types_available"] if not imported_types[key]
    ]
    fetched.summary = {
        "operations": operations,
        "notes": dict(sorted((+notes).items())),
        "note_selection": selection.summary,
        "complete": all(op["status"] == "ok" for op in operations if op["status"] != "skipped"),
        "telemetry": client.telemetry.describe(),
    }
    return fetched
