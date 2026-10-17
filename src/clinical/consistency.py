"""Deterministic contradiction and numeric-consistency checks across a record.

Two rule families, both citing every source involved:

* attribute contradictions - the same attribute documented with incompatible
  values in different sources (currently: smoking status);
* numeric mismatches - a lab value quoted in a note that matches no recorded
  Observation of that test for the patient.

These are narrow, explainable rules, not general contradiction detection.
"""

from __future__ import annotations

import re
from typing import Any

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
    return findings
