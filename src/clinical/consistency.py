"""Deterministic contradiction and numeric-consistency checks across a record.

Three rule families, each citing every source involved:

* attribute contradictions - the same attribute documented with incompatible
  values in different sources (currently: smoking status);
* numeric mismatches - a lab value quoted in a note that matches no recorded
  Observation of that test for the patient;
* encounter date conflicts - an admission or discharge date stated in a note
  (labelled, ISO format) that differs by more than one day from the start or
  end recorded on the encounter the note is linked to.

A stated date whose counterpart the linked encounter does not record (for
example a discharge date while the encounter has no end) is not a
contradiction. ``encounter_documentation_gaps`` reports it as incomplete
encounter documentation through the completeness checks.

These are narrow, explainable rules, not general contradiction detection. They
quote both values as recorded and never correct or infer a date.
"""

from __future__ import annotations

import re
from datetime import date
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # records.py imports this module for the completeness checks
    from src.clinical.records import ClinicalRecords

SMOKING_STATES = {
    "current smoker": re.compile(r"\bcurrent (every day )?smoker\b", re.I),
    "former smoker": re.compile(r"\bformer smoker\b|\bquit (smoking )?in \d{4}\b", re.I),
    "never smoker": re.compile(r"\bnever smoke[rd]\b", re.I),
}
LAB_MENTIONS = {
    "4548-4": re.compile(r"\bhba1c\b", re.I),
    "33914-3": re.compile(r"\begfr\b", re.I),
    "6301-6": re.compile(r"\binr\b", re.I),
    "2345-7": re.compile(r"\bglucose\b", re.I),
    "30934-4": re.compile(r"\bbnp\b", re.I),
    "2823-3": re.compile(r"\bpotassium\b", re.I),
    "718-7": re.compile(r"\bhemoglobin\b(?!\s*a1c)", re.I),
    "20150-9": re.compile(r"\bfev1\b", re.I),
    "59408-5": re.compile(r"\boxygen saturation\b", re.I),
}
VALUE_AFTER = re.compile(r"^([^0-9\n]{0,20}?)(\d+(?:\.\d+)?)")
COMPARATIVE = re.compile(r"\b(above|below|over|under|greater|less|than|target|range)\b", re.I)
STATED_ENCOUNTER_DATE = re.compile(
    r"\b(?:(?P<label>admission|admit|discharge)\s+date|date\s+of\s+(?P<label_of>admission|discharge))"
    r"\s*[:\-]?\s*(?P<date>\d{4}-\d{2}-\d{2})\b",
    re.I,
)
# Recorded encounter times are instants (often UTC); notes state local calendar dates.
ENCOUNTER_DATE_TOLERANCE_DAYS = 1
ENCOUNTER_FIELD = {"admission": "start", "discharge": "end"}


def find_inconsistencies(records: ClinicalRecords, patient_id: str) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    documents = [records.document_text(patient_id, d["id"]) for d in records.documents(patient_id)]
    documents = [d for d in documents if d]

    # 1. Smoking status across notes and social-history observations.
    states: dict[str, list[dict[str, Any]]] = {}
    for doc in documents:
        for state, pattern in SMOKING_STATES.items():
            match = pattern.search(doc["text"])
            if match:
                states.setdefault(state, []).append({**doc["source"], "excerpt": match.group(0),
                                                     "title": doc["title"], "date": doc["date"]})
    for obs in records.observations(patient_id, category="social-history"):
        value = str(obs["value"]).lower()
        state = "current smoker" if "current" in value else "former smoker" if "former" in value else None
        if state:
            states.setdefault(state, []).append({**obs["source"], "excerpt": str(obs["value"]),
                                                 "title": obs["name"], "date": obs["effective"]})
    if len(states) > 1:
        findings.append({
            "category": "attribute_contradiction",
            "attribute": "smoking status",
            "message": "Smoking status is documented inconsistently: "
                       + "; ".join(f"{state} ({len(srcs)} source{'s' if len(srcs) > 1 else ''})"
                                   for state, srcs in sorted(states.items())),
            "sources": [src for srcs in states.values() for src in srcs],
        })

    # 2. Lab values quoted in notes that match no recorded observation.
    recorded: dict[str, set[float]] = {}
    for obs in records.observations(patient_id):
        if isinstance(obs["value"], (int, float)):
            recorded.setdefault(obs["loinc"], set()).add(float(obs["value"]))
    for doc in documents:
        for loinc, pattern in LAB_MENTIONS.items():
            for match in pattern.finditer(doc["text"]):
                tail = VALUE_AFTER.match(doc["text"][match.end():])
                if not tail or COMPARATIVE.search(tail.group(1)):
                    continue
                value = float(tail.group(2))
                if loinc in recorded and value not in recorded[loinc]:
                    findings.append({
                        "category": "numeric_mismatch",
                        "attribute": match.group(0),
                        "message": f"'{doc['title']}' states {match.group(0)} {tail.group(2)}, but recorded "
                                   f"{match.group(0)} observations are "
                                   f"{', '.join(f'{v:g}' for v in sorted(recorded[loinc]))}.",
                        "sources": [{**doc["source"], "excerpt": f"{match.group(0)}{tail.group(0)}",
                                     "title": doc["title"], "date": doc["date"]}],
                    })

    # 3. Admission/discharge dates stated in a note that conflict with its linked encounter.
    for check in _encounter_date_checks(records, patient_id):
        if not check["conflicts"]:
            continue
        stated_text = " and ".join(f"{kind} date {value}" for kind, _, value, _ in check["conflicts"])
        recorded_text = " and ".join(f"{ENCOUNTER_FIELD[kind]} {value}" for kind, _, _, value in check["conflicts"])
        findings.append({
            "category": "encounter_date_conflict",
            "attribute": "encounter dates",
            "message": f"'{check['note']['title']}' documents {stated_text}, but its linked encounter records "
                       f"{recorded_text}. The two sources may conflict; both values are shown as recorded.",
            "sources": _encounter_check_sources(check, [fragment for _, fragment, _, _ in check["conflicts"]]),
        })
    return findings


def encounter_documentation_gaps(records: ClinicalRecords, patient_id: str) -> list[dict[str, Any]]:
    """Stated admission/discharge dates whose linked encounter records no start/end (completeness findings)."""
    findings: list[dict[str, Any]] = []
    for check in _encounter_date_checks(records, patient_id):
        if not check["gaps"]:
            continue
        encounter = check["encounter"]
        stated = " and ".join(f"{kind} date {value}" for kind, _, value in check["gaps"])
        missing = " or ".join(ENCOUNTER_FIELD[kind] for kind, _, _ in check["gaps"])
        findings.append({
            "category": "encounter_documentation_incomplete",
            "severity": "medium",
            "message": f"'{check['note']['title']}' documents {stated}, but its linked encounter (status "
                       f"{encounter['status']}) has no recorded {missing}. The encounter documentation may be "
                       "incomplete; this is not a confirmed contradiction.",
            "source": encounter["source"],
            "sources": _encounter_check_sources(check, [fragment for _, fragment, _ in check["gaps"]]),
        })
    return findings


def _calendar_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _encounter_date_checks(records: ClinicalRecords, patient_id: str) -> list[dict[str, Any]]:
    """Per note: labelled dates that conflict with, or have no counterpart on, the linked encounter.

    Only the patient's own encounters are considered. Recorded values are read from the stored
    resource as written (original offset kept) and are never modified.
    """
    encounters = {f"Encounter/{e['id']}": e for e in records.encounters(patient_id)}
    checks: list[dict[str, Any]] = []
    for meta in records.documents(patient_id):
        encounter = encounters.get(meta["encounter"] or "")
        doc = records.document_text(patient_id, meta["id"]) if encounter else None
        if encounter is None or not doc:
            continue
        period = (records.store.get("Encounter", encounter["id"]) or {}).get("period") or {}
        conflicts: list[tuple[str, str, str, str]] = []  # kind, stated fragment, stated date, recorded value
        gaps: list[tuple[str, str, str]] = []  # kind, stated fragment, stated date
        seen: set[str] = set()
        for match in STATED_ENCOUNTER_DATE.finditer(doc["text"]):
            label = (match.group("label") or match.group("label_of")).lower()
            kind = "discharge" if label == "discharge" else "admission"
            stated = _calendar_date(match.group("date"))
            if stated is None or kind in seen:
                continue
            seen.add(kind)
            recorded = period.get(ENCOUNTER_FIELD[kind])
            if not recorded:
                gaps.append((kind, match.group(0), match.group("date")))
                continue
            recorded_day = _calendar_date(str(recorded))
            if recorded_day is not None and abs((stated - recorded_day).days) > ENCOUNTER_DATE_TOLERANCE_DAYS:
                conflicts.append((kind, match.group(0), match.group("date"), str(recorded)))
        if conflicts or gaps:
            checks.append({"note": doc, "encounter": encounter, "period": period,
                           "conflicts": conflicts, "gaps": gaps})
    return checks


def _encounter_check_sources(check: dict[str, Any], fragments: list[str]) -> list[dict[str, Any]]:
    note, encounter, period = check["note"], check["encounter"], check["period"]
    recorded = (f"period start {period.get('start') or 'not recorded'}, end {period.get('end') or 'not recorded'}, "
                f"status {encounter['status'] or 'not recorded'}")
    return [
        {**note["source"], "excerpt": "; ".join(fragments), "title": note["title"], "date": note["date"]},
        {**encounter["source"], "excerpt": recorded, "title": "Linked encounter", "date": period.get("start") or ""},
    ]
