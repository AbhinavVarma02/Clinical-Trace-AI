"""Trusted encounter intervals are computed from patient-scoped source versions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from evaluation.reliability import citation_checks
from src.agent.tools import NoArgs, ToolContext, _encounters, run_tool
from src.safety.policy import PERSONAS, AccessContext


def context(runtime, patient_id="synthetic_p001"):
    return ToolContext(
        AccessContext(PERSONAS["clinician.demo"]), runtime.policy, runtime.records, runtime.index, patient_id
    )


def encounter(identifier, start, end, version="1", kind="IMP"):
    return {
        "id": identifier,
        "class": kind,
        "class_display": "inpatient encounter",
        "start": start,
        "end": end,
        "length_of_stay_days": 1 if end else None,
        "reason": "recorded reason",
        "source": {"reference": f"Encounter/{identifier}", "version": version},
    }


@pytest.mark.parametrize("gap,within", [(0, True), (30, True), (31, False)])
def test_generic_calendar_day_boundaries_preserve_both_versions(runtime, monkeypatch, gap, within):
    discharged = datetime(2025, 1, 2, 10, tzinfo=timezone.utc)
    admitted = discharged + timedelta(days=gap, hours=2)
    items = [
        encounter("prior-record", "2025-01-01T10:00:00Z", discharged.isoformat(), "3"),
        encounter("next-record", admitted.isoformat(), (admitted + timedelta(days=1)).isoformat(), "7"),
    ]
    monkeypatch.setattr(runtime.records, "encounters", lambda patient: items)
    data, evidence = _encounters(context(runtime), NoArgs())
    assert data is items
    intervals = [e for e in evidence if e.kind == "encounter_interval"]
    assert len(intervals) == 1
    interval = intervals[0]
    assert interval.evidence_id == "check:encounter_interval:prior-record:next-record"
    assert interval.patient_id == "synthetic_p001"
    assert interval.source_ref == "Encounter/next-record" and interval.source_version == "7"
    assert f"began {gap} days after discharge on 2025-01-02" in interval.text
    assert admitted.date().isoformat() in interval.text
    assert "Encounter/prior-record@v3" in interval.text and "Encounter/next-record@v7" in interval.text
    assert ("meets the thirty-day readmission definition" in interval.text) is within
    assert ("outside the thirty-day readmission window" in interval.text) is not within


@pytest.mark.parametrize(
    "previous_end,current_start",
    [
        (None, "2025-02-01T12:00:00Z"),
        ("2025-02-02T12:00:00Z", None),
        ("2025-02-02T12:00:00Z", "2025-02-01T12:00:00Z"),
        ("2025-02-02T12:00:00Z", "2025-02-02T11:00:00Z"),
    ],
)
def test_missing_dates_and_overlaps_produce_no_interval(runtime, monkeypatch, previous_end, current_start):
    items = [
        encounter("prior", "2025-01-01T10:00:00Z", previous_end),
        encounter("next", current_start, "2025-02-03T12:00:00Z"),
    ]
    monkeypatch.setattr(runtime.records, "encounters", lambda patient: items)
    data, evidence = _encounters(context(runtime), NoArgs())
    assert data is items
    assert not any(e.kind == "encounter_interval" for e in evidence)


def test_only_consecutive_inpatient_pairs_are_compared(runtime, monkeypatch):
    items = [
        encounter("a", "2025-01-01T00:00:00Z", "2025-01-02T00:00:00Z"),
        encounter("clinic", "2025-01-10T00:00:00Z", "2025-01-10T01:00:00Z", kind="AMB"),
        encounter("b", "2025-01-20T00:00:00Z", "2025-01-21T00:00:00Z"),
        encounter("c", "2025-03-01T00:00:00Z", "2025-03-02T00:00:00Z"),
    ]
    monkeypatch.setattr(runtime.records, "encounters", lambda patient: items)
    data, evidence = _encounters(context(runtime), NoArgs())
    assert data is items
    assert [e.evidence_id for e in evidence if e.kind == "encounter_interval"] == [
        "check:encounter_interval:a:b",
        "check:encounter_interval:b:c",
    ]


def test_real_fixture_interval_lineage_and_citation_resolution(runtime):
    patient_id = "synthetic_p001"
    result = run_tool(context(runtime, patient_id), "get_patient_encounters")
    intervals = [e for e in result.evidence if e.kind == "encounter_interval"]
    assert intervals
    inpatient = [enc for enc in result.data if enc["class"] == "IMP"]
    expected_pairs = list(zip(inpatient, inpatient[1:]))
    assert len(intervals) == len(expected_pairs)
    for interval, (previous, current) in zip(intervals, expected_pairs):
        assert interval.patient_id == patient_id
        assert interval.source_ref == current["source"]["reference"]
        assert interval.source_version == current["source"]["version"]
        for encounter_record in (previous, current):
            source = encounter_record["source"]
            assert f"{source['reference']}@v{source['version']}" in interval.text
        artifact = {
            "patient_id": patient_id,
            "answer": {"claims": [{"text": interval.text, "citations": [interval.evidence_id]}]},
            "evidence": [interval.as_dict()],
        }
        assert citation_checks(runtime, artifact)["valid"]
        artifact["patient_id"] = "synthetic_p002"
        assert not citation_checks(runtime, artifact)["valid"]


def test_intervals_do_not_mix_patients(runtime):
    for patient_id in runtime.store.patient_ids():
        result = run_tool(context(runtime, patient_id), "get_patient_encounters")
        for interval in (e for e in result.evidence if e.kind == "encounter_interval"):
            assert interval.patient_id == patient_id
            identifier = interval.source_ref.split("/", 1)[1]
            resource = runtime.store.get("Encounter", identifier, int(interval.source_version))
            assert resource["subject"]["reference"] == f"Patient/{patient_id}"
