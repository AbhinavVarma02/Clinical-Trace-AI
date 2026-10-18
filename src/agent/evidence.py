"""Preserve evidence identity and prioritize documented journey events.

Selection changes model input only. Every output claim still passes the same
verifier; no selected event is inserted into a model answer by the application.
"""

from __future__ import annotations

import re
from dataclasses import replace

from src.safety.guards import flag_content
from src.safety.verifier import INTERVAL_SOURCE, EvidenceItem, verify_claims

TERMINAL_ORDER = re.compile(r"; status (stopped|completed|cancelled|on-hold);", re.I)


def merge_evidence(existing: EvidenceItem | None, incoming: EvidenceItem) -> EvidenceItem:
    """Promote a timeline view to its structured record without losing content.

    Tool order must not decide the clinical kind/date of a versioned source.
    Copy rather than mutate tool results; retain all flags and source views.
    """
    if existing is None:
        return replace(incoming, flags=sorted(set(incoming.flags + flag_content(incoming.text))))
    if (existing.evidence_id, existing.patient_id, existing.source_ref, existing.source_version) != (
        incoming.evidence_id,
        incoming.patient_id,
        incoming.source_ref,
        incoming.source_version,
    ):
        raise ValueError("shared evidence id has conflicting patient/source/version identity")
    primary, other = (
        (incoming, existing) if existing.kind == "timeline" and incoming.kind != "timeline" else (existing, incoming)
    )
    text = primary.text if other.text in primary.text else f"{primary.text} | {other.text}"
    flags = sorted(set(primary.flags + other.flags + flag_content(text)))
    return replace(primary, text=text, flags=flags)


def interval_statement(item: EvidenceItem) -> str | None:
    """A short view of the existing trusted computation, with its full proof kept.

    Only an already-verifiable complete tool result can supply this view. It
    retains both event dates, the gap and the window classification. Source
    lineage remains in the full evidence map shown to verification and review.
    """
    if item.kind != "encounter_interval" or item.flags or flag_content(item.text):
        return None
    source = INTERVAL_SOURCE.fullmatch(item.text)
    if source is None:
        return None
    claim = {"text": item.text, "citations": [item.evidence_id]}
    if verify_claims([claim], {item.evidence_id: item}, item.patient_id)["status"] != "verified":
        return None
    return item.text.split(". Sources:", 1)[0].replace(" (0 to 30 days)", "") + "."


def summary_focus(evidence: dict[str, EvidenceItem]) -> list[EvidenceItem]:
    """Prioritize actual return admissions and dated non-active order events.

    No patient identifiers, medication names or expected answers are encoded.
    A routine active order does not become a change, and a long admission gap
    does not become a return within the defined window.
    """
    usable = [e for e in evidence.values() if not e.flags and not flag_content(e.text)]
    intervals = []
    for item in usable:
        if item.kind != "encounter_interval" or not interval_statement(item):
            continue
        source = INTERVAL_SOURCE.fullmatch(item.text)
        if source is not None and int(source["gap"]) <= 30:
            intervals.append(item)
    intervals.sort(key=lambda e: (e.date, e.evidence_id), reverse=True)
    medications = [e for e in usable if e.kind == "medication" and TERMINAL_ORDER.search(e.text)]
    medications.sort(key=lambda e: (e.date, e.evidence_id), reverse=True)
    distinct = []
    seen = set()
    for medication in medications:
        if medication.source_ref not in seen:
            distinct.append(medication)
            seen.add(medication.source_ref)
    return intervals[:1] + distinct[:2]
