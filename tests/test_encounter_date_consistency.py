"""Note-stated admission/discharge dates versus the linked encounter (ISSUE-5). Offline; invented records only.

Two findings are kept apart:

* a documented admission (or discharge) date that differs from the linked encounter's recorded start (or end)
  by more than one day is a *possible inconsistency* (``find_inconsistencies``);
* a documented discharge (or admission) date whose linked encounter records no end (or start) is *incomplete
  encounter documentation*, not a contradiction (``encounter_documentation_gaps``, reported through the
  completeness checks).

Both cite the note and the encounter, quote both original values as recorded, and never change a date.
"""

from __future__ import annotations

import base64
import copy
import json
import re
import uuid

import pytest

from src.agent.tools import ToolContext, run_tool
from src.clinical.consistency import encounter_documentation_gaps, find_inconsistencies
from src.clinical.records import ClinicalRecords
from src.clinical.store import FHIRStore
from src.clinical.synthetic import FIXTURE_PATH
from src.persistence.db import Database
from src.runtime import build_runtime
from src.safety.policy import PERSONAS, AccessContext, Persona

PID = "synthetic_edc001"
OTHER = "synthetic_edc002"
META = {"versionId": "1", "lastUpdated": "2026-11-01T00:00:00Z",
        "tag": [{"system": "urn:clinical-trace:data-origin", "code": "synthetic"}]}
ENC = "Encounter/edc-enc-2019"
DS = "DocumentReference/edc-doc-ds"
ISSUE5_TEXT = ("HOSPITAL DISCHARGE SUMMARY\nAdmission date: 2026-08-05\nDischarge date: 2026-08-07\n"
               "FOLLOW-UP Cardiology clinic within 7 days of discharge.")
ISO = re.compile(r"\d{4}-\d{2}-\d{2}")


def patient(pid: str = PID) -> dict:
    return {"resourceType": "Patient", "id": pid, "meta": META, "gender": "male", "birthDate": "1950-01-01",
            "name": [{"text": "Invented Test (synthetic record)"}]}


def encounter(eid: str = "edc-enc-2019", start: str | None = "2019-05-28T15:22:00Z", end: str | None = None,
              status: str = "in-progress", pid: str = PID) -> dict:
    period = {key: value for key, value in (("start", start), ("end", end)) if value}
    return {"resourceType": "Encounter", "id": eid, "meta": META, "status": status,
            "class": {"system": "urn:oid:invented", "code": "3", "display": "Admission"},
            "subject": {"reference": f"Patient/{pid}"}, "period": period}


def note(did: str = "edc-doc-ds", text: str = ISSUE5_TEXT, date: str = "2026-11-02T05:03:06Z",
         encounter_ref: str | None = ENC, title: str = "Discharge Summary", pid: str = PID) -> dict:
    document = {"resourceType": "DocumentReference", "id": did, "meta": META, "status": "current",
                "type": {"text": title}, "subject": {"reference": f"Patient/{pid}"}, "date": date,
                "content": [{"attachment": {"contentType": "text/plain",
                                            "data": base64.b64encode(text.encode()).decode()}}]}
    if encounter_ref:
        document["context"] = {"encounter": [{"reference": encounter_ref}]}
    return document


def records_for(*resources: dict) -> ClinicalRecords:
    store = FHIRStore(Database(":memory:"))
    report = store.ingest([patient(), *resources], source="test")
    assert report.invalid == 0, report.as_dict()
    return ClinicalRecords(store)


def refs(finding: dict) -> list[str]:
    return [source["reference"] for source in finding["sources"]]


# ------------------------------------------------------------------------- the two findings are distinct
def test_issue5_shape_gives_one_conflict_and_one_documentation_gap():
    records = records_for(encounter(), note())
    conflicts = find_inconsistencies(records, PID)
    gaps = encounter_documentation_gaps(records, PID)

    assert [f["category"] for f in conflicts] == ["encounter_date_conflict"]
    conflict = conflicts[0]
    assert "admission date 2026-08-05" in conflict["message"]
    assert "start 2019-05-28T15:22:00Z" in conflict["message"]
    assert "may conflict" in conflict["message"] and "discharge date" not in conflict["message"].lower()
    assert "2026-08-07" not in conflict["message"]
    assert refs(conflict) == [DS, ENC]
    assert conflict["sources"][0]["excerpt"] == "Admission date: 2026-08-05"  # verbatim from the note

    assert [f["category"] for f in gaps] == ["encounter_documentation_incomplete"]
    gap = gaps[0]
    assert "discharge date 2026-08-07" in gap["message"] and "no recorded end" in gap["message"]
    assert "not a confirmed contradiction" in gap["message"] and "2026-08-05" not in gap["message"]
    assert gap["source"]["reference"] == ENC and refs(gap) == [DS, ENC]
    assert gap["sources"][0]["excerpt"] == "Discharge date: 2026-08-07"
    assert "end not recorded" in gap["sources"][1]["excerpt"]

    # The gap is reported by the completeness checks, never as an inconsistency.
    missing = records.missing_information(PID)
    assert missing[-1] == gap
    assert not any(f["category"].startswith("encounter_documentation") for f in conflicts)
    assert not any(f["category"] == "encounter_date_conflict" for f in missing)


def test_matching_dates_give_no_finding():
    records = records_for(encounter(start="2026-08-05T14:00:00Z", end="2026-08-07T16:00:00Z", status="finished"),
                          note())
    assert find_inconsistencies(records, PID) == [] and encounter_documentation_gaps(records, PID) == []


@pytest.mark.parametrize("start,stated,expected", [
    ("2026-08-05T23:30:00Z", "2026-08-06", 0),        # UTC instant vs local calendar date: within one day
    ("2026-08-07T00:30:00Z", "2026-08-06", 0),
    ("2026-08-05T21:30:00-05:00", "2026-08-04", 0),   # recorded value's own date (UTC would be 2 days apart)
    ("2026-08-04T10:00:00Z", "2026-08-06", 1),        # two days apart
    ("2026-08-09T10:00:00Z", "2026-08-06", 1),
])
def test_the_one_day_tolerance(start, stated, expected):
    records = records_for(encounter(start=start, end="2026-08-10T10:00:00Z", status="finished"),
                          note(text=f"Admission date: {stated}"))
    assert len(find_inconsistencies(records, PID)) == expected
    assert encounter_documentation_gaps(records, PID) == []


def test_a_discharge_date_that_differs_from_a_recorded_end_is_an_inconsistency_not_a_gap():
    records = records_for(encounter(start="2026-08-05T14:00:00Z", end="2026-08-07T16:00:00Z", status="finished"),
                          note(text="Admission date: 2026-08-05\nDischarge date: 2026-08-12"))
    conflicts = find_inconsistencies(records, PID)
    assert len(conflicts) == 1 and encounter_documentation_gaps(records, PID) == []
    message = conflicts[0]["message"]
    assert "discharge date 2026-08-12" in message and "end 2026-08-07T16:00:00Z" in message
    assert "admission date" not in message  # the matching admission date is not reported
    assert conflicts[0]["sources"][0]["excerpt"] == "Discharge date: 2026-08-12"


def test_original_values_are_quoted_verbatim_and_never_corrected():
    records = records_for(encounter(start="2019-05-28T10:22:00-05:00"), note())
    message = find_inconsistencies(records, PID)[0]["message"]
    assert "2019-05-28T10:22:00-05:00" in message and "2026-08-05" in message  # not normalised to UTC
    assert "both values are shown as recorded" in message


def test_notes_without_labelled_iso_dates_are_not_flagged():
    texts = [
        "Progress note: stable on review.",  # a 2026 note linked to the 2019 open encounter
        "Admitted on 5 August 2026 and discharged 08/07/2026.",  # unlabelled / non-ISO
        "Admission date: 2026-02-30\nDischarge date: 2026-13-01",  # invalid calendar dates
    ]
    records = records_for(encounter(), *[note(f"edc-doc-{n}", text=t, title="Progress Notes")
                                         for n, t in enumerate(texts)])
    assert find_inconsistencies(records, PID) == [] and encounter_documentation_gaps(records, PID) == []


def test_unlinked_notes_and_unresolved_encounter_links_are_ignored():
    records = records_for(encounter(), note("edc-doc-a", encounter_ref=None),
                          note("edc-doc-b", encounter_ref="Encounter/edc-enc-absent"))
    assert find_inconsistencies(records, PID) == [] and encounter_documentation_gaps(records, PID) == []


def test_another_patients_encounter_is_never_used():
    store = FHIRStore(Database(":memory:"))
    store.ingest([patient(), patient(OTHER), encounter(), note("edc-doc-other", pid=OTHER)], source="test")
    records = ClinicalRecords(store)
    # The other patient's note points at this patient's encounter: no finding for either patient.
    assert find_inconsistencies(records, OTHER) == [] and encounter_documentation_gaps(records, OTHER) == []
    assert find_inconsistencies(records, PID) == [] and encounter_documentation_gaps(records, PID) == []


def test_source_facts_are_unchanged_and_every_reported_date_comes_from_a_source():
    records = records_for(encounter(), note())
    before = (copy.deepcopy(records.documents(PID)), copy.deepcopy(records.encounters(PID)),
              copy.deepcopy(records.store.get("Encounter", "edc-enc-2019")),
              copy.deepcopy(records.store.get("DocumentReference", "edc-doc-ds")))
    findings = find_inconsistencies(records, PID) + encounter_documentation_gaps(records, PID)
    records.missing_information(PID)
    assert (records.documents(PID), records.encounters(PID), records.store.get("Encounter", "edc-enc-2019"),
            records.store.get("DocumentReference", "edc-doc-ds")) == before
    source_text = ISSUE5_TEXT + json.dumps(records.store.get("Encounter", "edc-enc-2019")["period"])
    for finding in findings:
        assert ISO.findall(finding["message"]) and all(d in source_text for d in ISO.findall(finding["message"]))


def test_the_synthetic_demo_bundle_gains_no_encounter_date_findings():
    store = FHIRStore(Database(":memory:"))
    store.ingest_bundle_file(FIXTURE_PATH)
    records = ClinicalRecords(store)
    for pid in store.patient_ids():
        assert not any(f["category"] == "encounter_date_conflict" for f in find_inconsistencies(records, pid))
        assert encounter_documentation_gaps(records, pid) == []
        assert not any("sources" in f for f in records.missing_information(pid))


# ------------------------------------------------------------------------- through the unchanged agent
def _runtime(tmp_path, resources):
    bundle = tmp_path / "bundle.json"
    bundle.write_text(json.dumps({"resourceType": "Bundle", "type": "collection",
                                  "entry": [{"resource": r} for r in resources]}), encoding="utf-8")
    return build_runtime(":memory:", fixture=bundle, provider=None, enable_model=False)


@pytest.fixture()
def agent_runtime(tmp_path):
    rt = _runtime(tmp_path, [patient(), patient(OTHER), encounter(), note(),
                             note("edc-doc-other", pid=OTHER, title="Discharge Summary")])
    yield rt
    rt.close()


def _access(*panel: str) -> AccessContext:
    persona = Persona("edc.clinician", "Invented test clinician", "clinician", tuple(panel))
    return AccessContext(persona=persona, request_id=str(uuid.uuid4()))


def _evidence(result: dict) -> dict[str, dict]:
    return {item["evidence_id"]: item for item in result["evidence"]}


def test_the_gap_cites_both_resources_and_is_not_an_inconsistency(agent_runtime):
    result = agent_runtime.agent.run(_access(PID), PID, "Is any documentation missing?")
    assert result["outcome"] == "answered" and result["inference"]["mode"] == "deterministic"
    assert result["verification"]["status"] == "verified" and result["answer"]["rejected_claims"] == []
    assert result["verification"]["isolation_violations"] == 0
    gaps = [c for c in result["answer"]["claims"] if "no recorded end" in c["text"]]
    assert len(gaps) == 1
    claim = gaps[0]
    assert claim["text"].startswith("Documentation gap: ") and "Possible inconsistency" not in claim["text"]
    evidence = _evidence(result)
    assert claim["citations"][0].startswith("check:encounter_documentation_incomplete:")
    cited = [evidence[c] for c in claim["citations"]]
    assert [e["source_ref"] for e in cited] == [ENC, DS, ENC]
    assert all(e["patient_id"] == PID for e in cited)
    for item in cited:  # every citation resolves to a stored resource of this patient
        rtype, rid = item["source_ref"].split("/", 1)
        resource = agent_runtime.store.get(rtype, rid)
        assert resource is not None and resource.get("subject", {}).get("reference", f"Patient/{PID}") == f"Patient/{PID}"
    assert "the record contains inconsistencies" not in result["review"]["reasons"]
    assert not any(c["text"].startswith("Possible inconsistency") for c in result["answer"]["claims"])


def test_the_conflict_is_a_possible_inconsistency_and_triggers_review(agent_runtime):
    result = agent_runtime.agent.run(_access(PID), PID, "Are there any inconsistencies in the record?")
    assert result["outcome"] == "answered" and result["verification"]["status"] == "verified"
    inconsistencies = [c for c in result["answer"]["claims"] if c["text"].startswith("Possible inconsistency")]
    assert len(inconsistencies) == 1
    claim = inconsistencies[0]
    assert "admission date 2026-08-05" in claim["text"] and "2019-05-28T15:22:00Z" in claim["text"]
    assert "2026-08-07" not in claim["text"]  # the open-encounter discharge date is a gap, not a conflict
    evidence = _evidence(result)
    assert [evidence[c]["source_ref"] for c in claim["citations"]] == [DS, DS, ENC]
    assert all(evidence[c]["patient_id"] == PID for c in claim["citations"])
    assert not any(c["text"].startswith("Documentation gap") for c in result["answer"]["claims"])
    assert "the record contains inconsistencies" in result["review"]["reasons"]


def test_another_patient_sees_neither_finding(agent_runtime):
    for question in ("Is any documentation missing?", "Are there any inconsistencies in the record?"):
        result = agent_runtime.agent.run(_access(OTHER), OTHER, question)
        text = " ".join(c["text"] for c in result["answer"]["claims"])
        assert "2019-05-28" not in text and "no recorded end" not in text
        assert all(e["patient_id"] == OTHER for e in result["evidence"])


def test_a_single_source_gap_still_keeps_its_own_note_out_of_the_quotes(tmp_path):
    closed = encounter("edc-enc-closed", start="2026-08-05T14:00:00Z", end="2026-08-07T16:00:00Z", status="finished")
    flagged = note("edc-doc-flagged", title="Progress Notes", encounter_ref="Encounter/edc-enc-closed",
                   text="Follow-up plan: cardiology clinic in two weeks. Medication reconciliation not documented.")
    plain = note("edc-doc-plain", title="Progress Notes", encounter_ref="Encounter/edc-enc-closed",
                 text="Follow-up plan: primary care visit next month.")
    rt = _runtime(tmp_path, [patient(), closed, flagged, plain])
    try:
        result = rt.agent.run(_access(PID), PID, "What follow-up is planned, and is anything missing?")
    finally:
        rt.close()
    assert result["verification"]["status"] == "verified" and result["answer"]["rejected_claims"] == []
    evidence = _evidence(result)
    gaps = [c for c in result["answer"]["claims"] if c["kind"] == "missing_information" and "not documented" in c["text"]]
    assert len(gaps) == 1 and len(gaps[0]["citations"]) == 1
    assert gaps[0]["citations"][0].startswith("check:note_incomplete:")
    assert evidence[gaps[0]["citations"][0]]["source_ref"] == "DocumentReference/edc-doc-flagged"
    retrieved = {e["source_ref"] for e in result["evidence"] if e["kind"] == "retrieved"}
    quoted = {evidence[c]["source_ref"] for claim in result["answer"]["claims"] if claim["kind"] == "quote"
              for c in claim["citations"]}
    assert "DocumentReference/edc-doc-flagged" in retrieved  # found by retrieval ...
    assert "DocumentReference/edc-doc-flagged" not in quoted  # ... but not re-quoted: existing deduplication
    assert "DocumentReference/edc-doc-plain" in quoted


def test_single_source_documentation_gaps_are_unchanged(runtime):
    access = AccessContext(PERSONAS["clinician.demo"])
    for pid in runtime.store.patient_ids():
        findings = runtime.records.missing_information(pid)
        ctx = ToolContext(access=access, policy=runtime.policy, records=runtime.records, index=runtime.index,
                          patient_id=pid, predictor=None)
        tool = run_tool(ctx, "detect_missing_information", {})
        assert tool.ok and [e.evidence_id for e in tool.evidence] == [
            f"check:{f['category']}:{n}" for n, f in enumerate(findings, start=1)]
        result = runtime.agent.run(access, pid, "What information is missing from this record?")
        gaps = [c for c in result["answer"]["claims"] if c["kind"] == "missing_information"]
        assert [(c["text"], c["citations"]) for c in gaps] == [
            (f"Documentation gap: {f['message']}", [f"check:{f['category']}:{n}"])
            for n, f in enumerate(findings, start=1)]
