"""Discharge-summary detection for the inpatient completeness check (P2). Offline; invented records only.

A note counts as the discharge summary of its linked encounter when its type carries LOINC 18842-5, or when its
type text or a coding display reads "discharge summary" in any letter case or spacing. Nothing looser counts
("Discharge instructions", "Discharge summary addendum", the code under another system), so a real gap is still
reported. The check still runs only for encounter class ``IMP``.
"""

from __future__ import annotations

import base64

import pytest

from src.clinical import records
from src.clinical.fhir_models import display_text
from src.clinical.records import ClinicalRecords
from src.clinical.store import FHIRStore
from src.clinical.synthetic import FIXTURE_PATH
from src.persistence.db import Database
from src.runtime import build_runtime

PID = "synthetic_dsd001"
META = {
    "versionId": "1",
    "lastUpdated": "2026-11-01T00:00:00Z",
    "tag": [{"system": "urn:clinical-trace:data-origin", "code": "synthetic"}],
}
LOINC = "http://loinc.org"
V3 = "http://terminology.hl7.org/CodeSystem/v3-ActCode"


def patient() -> dict:
    return {
        "resourceType": "Patient",
        "id": PID,
        "meta": META,
        "gender": "female",
        "birthDate": "1960-01-01",
        "name": [{"text": "Invented Test (synthetic record)"}],
    }


def encounter(
    eid: str = "dsd-enc-1", code: str = "IMP", start: str = "2025-03-02T08:00:00Z", end: str = "2025-03-06T15:00:00Z"
) -> dict:
    return {
        "resourceType": "Encounter",
        "id": eid,
        "meta": META,
        "status": "finished",
        "class": {"system": V3, "code": code},
        "subject": {"reference": f"Patient/{PID}"},
        "period": {"start": start, "end": end},
    }


def note(type_concept: dict, did: str = "dsd-doc-1", encounter_id: str | None = "dsd-enc-1") -> dict:
    document = {
        "resourceType": "DocumentReference",
        "id": did,
        "meta": META,
        "status": "current",
        "type": type_concept,
        "subject": {"reference": f"Patient/{PID}"},
        "date": "2025-03-06T16:00:00Z",
        "content": [
            {
                "attachment": {
                    "contentType": "text/plain",
                    "data": base64.b64encode(b"Hospital course: treated.").decode(),
                }
            }
        ],
    }
    if encounter_id:
        document["context"] = {"encounter": [{"reference": f"Encounter/{encounter_id}"}]}
    return document


def missing_discharge(*resources: dict) -> list[dict]:
    store = FHIRStore(Database(":memory:"))
    report = store.ingest([patient(), *resources], source="test")
    assert report.invalid == 0, report.as_dict()
    findings = ClinicalRecords(store).missing_information(PID)
    return [f for f in findings if f["category"] == "discharge_summary_missing"]


RECOGNISED = {
    "epic_style_text_with_loinc": {
        "text": "Discharge Summary",
        "coding": [{"system": LOINC, "code": "18842-5", "display": "Discharge summary"}],
    },
    "existing_synthetic_label": {
        "text": "Discharge summary",
        "coding": [{"system": LOINC, "code": "18842-5", "display": "Discharge summary"}],
    },
    "text_only_upper_case_and_spacing": {"text": "  DISCHARGE   SUMMARY "},
    "loinc_only_without_any_label": {"coding": [{"system": LOINC, "code": "18842-5"}]},
    "loinc_with_a_different_label": {
        "text": "Hospital discharge note",
        "coding": [{"system": LOINC, "code": "18842-5"}],
    },
    "coding_display_only": {"coding": [{"system": "urn:oid:invented", "code": "5", "display": "Discharge Summary"}]},
}

NOT_RECOGNISED = {
    "discharge_instructions": {"text": "Discharge instructions"},
    "addendum_is_not_the_summary": {"text": "Discharge summary addendum"},
    "progress_note_loinc": {"text": "Progress note", "coding": [{"system": LOINC, "code": "11506-3"}]},
    "code_under_another_system": {"coding": [{"system": "http://snomed.info/sct", "code": "18842-5"}]},
    "summary_without_discharge": {"text": "Summary"},
}


@pytest.mark.parametrize("concept", RECOGNISED.values(), ids=RECOGNISED.keys())
def test_a_linked_discharge_summary_is_recognised(concept):
    assert missing_discharge(encounter(), note(concept)) == []


@pytest.mark.parametrize("concept", NOT_RECOGNISED.values(), ids=NOT_RECOGNISED.keys())
def test_other_note_types_do_not_count_as_a_discharge_summary(concept):
    findings = missing_discharge(encounter(), note(concept))
    assert [f["source"]["reference"] for f in findings] == ["Encounter/dsd-enc-1"]


def test_an_inpatient_stay_without_any_note_is_still_reported():
    findings = missing_discharge(encounter())
    assert len(findings) == 1
    assert findings[0]["severity"] == "high"
    assert findings[0]["message"] == "No discharge summary found for inpatient encounter dsd-enc-1."


def test_a_summary_covers_only_the_encounter_it_is_linked_to():
    epic_style = RECOGNISED["epic_style_text_with_loinc"]
    second = encounter("dsd-enc-2", start="2025-04-01T08:00:00Z", end="2025-04-03T12:00:00Z")
    assert [f["source"]["reference"] for f in missing_discharge(encounter(), second, note(epic_style))] == [
        "Encounter/dsd-enc-2"
    ]
    assert len(missing_discharge(encounter(), note(epic_style, encounter_id=None))) == 1


@pytest.mark.parametrize("code", ["AMB", "3"])
def test_the_check_still_runs_only_for_class_imp(code):
    # Epic's own class code "3" is not mapped to inpatient (no documented code system; see docs/EPIC_SANDBOX.md).
    assert missing_discharge(encounter(code=code)) == []


def test_synthetic_notes_classify_exactly_as_before():
    """The 14 synthetic notes: the new rule agrees with the old exact match on every one."""
    runtime = build_runtime(":memory:", fixture=FIXTURE_PATH, provider=None, enable_model=False)
    try:
        notes = [
            doc
            for pid in runtime.store.patient_ids()
            for doc in runtime.store.all_for_patient(pid, "DocumentReference")
        ]
        assert len(notes) == 14
        old = [display_text(doc.get("type")) == "Discharge summary" for doc in notes]
        assert [records.is_discharge_summary(doc) for doc in notes] == old
        assert sum(old) == 4
        categories = {
            pid: [f["category"] for f in runtime.records.missing_information(pid)]
            for pid in runtime.store.patient_ids()
        }
    finally:
        runtime.close()
    assert categories == {
        "synthetic_p001": [],
        "synthetic_p002": [],
        "synthetic_p003": [
            "encounter_end_missing",
            "discharge_summary_missing",
            "unit_missing",
            "medication_list_missing",
            "note_incomplete",
            "note_incomplete",
            "note_incomplete",
            "note_incomplete",
        ],
        "synthetic_p004": [],
    }
