"""Score accepted guided-event coverage; this never generates/replays inference.

Expected source identities and event dates are evaluation annotations only, not
inputs to the agent. Mentions and citation-grounded event coverage are separate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from src.safety.verifier import EvidenceItem, _dates, verify_claims


@dataclass(frozen=True)
class ExpectedJourney:
    patient_id: str
    admission_ref: str
    stop_ref: str
    stop_version: str
    medication: str
    stopped_on: str


def coverage(result: dict[str, Any], expected: ExpectedJourney) -> dict[str, bool]:
    claims = result.get("answer", {}).get("claims", [])
    evidence = {
        e["evidence_id"]: EvidenceItem(**{k: v for k, v in e.items() if k != "cited"})
        for e in result.get("evidence", [])
    }
    output = {
        "readmission_mention": False,
        "medication_mention": False,
        "readmission_grounded": False,
        "medication_stop_grounded": False,
    }
    if result.get("patient_id") != expected.patient_id:
        return output
    stopped_on = date.fromisoformat(expected.stopped_on)
    for claim in claims:
        text = claim.get("text", "")
        readmission = bool(re.search(r"\breadmi(?:ssion|tted)\b", text, re.I))
        medication = bool(re.search(rf"\b{re.escape(expected.medication)}\b", text, re.I))
        output["readmission_mention"] |= readmission
        output["medication_mention"] |= medication
        if re.search(r"\b(?:no|not|without)\b", text, re.I):
            continue
        if verify_claims([dict(claim)], evidence, expected.patient_id)["status"] != "verified":
            continue
        cited = [evidence[c] for c in claim.get("citations", []) if c in evidence]
        # A full interval statement must pass the unchanged typed computation
        # verifier, rather than borrowing the word 'readmission' from a source.
        if readmission and any(
            e.kind == "encounter_interval"
            and e.source_ref == expected.admission_ref
            and "this interval meets the thirty-day" in e.text
            for e in cited
        ):
            output["readmission_grounded"] = True
        dates = [value for _, value in _dates(text)]
        if (
            medication
            and re.search(r"\bstopped\b", text, re.I)
            and (stopped_on.year, stopped_on.month, stopped_on.day) in dates
            and any(
                e.kind == "medication"
                and e.source_ref == expected.stop_ref
                and e.source_version == expected.stop_version
                for e in cited
            )
        ):
            output["medication_stop_grounded"] = True
    return output
