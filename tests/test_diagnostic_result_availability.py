"""M2.2-c result-reference availability. Invented records and MOCK Epic fixtures only."""

from __future__ import annotations

import base64
import copy

import pytest

from src.agent.tools import ToolContext, run_tool
from src.clinical.records import ClinicalRecords, source_of
from src.clinical.store import FHIRStore
from src.persistence.db import Database
from src.safety.policy import PERSONAS, AccessContext
from src.sources import source_provenance
from src.interop.epic import settings, workspace
from tests.epic_support import RETRIEVED_AT, fixture_patient

PID = "synthetic_dra001"
OTHER = "synthetic_dra002"
DATE = "2025-03-06T10:30:00Z"
CATEGORY = "diagnostic_result_unavailable"
META = {"versionId": "2", "lastUpdated": "2026-11-01T00:00:00Z"}
QUESTIONS = (
    "Is any documentation missing?",
    "Summarize this patient journey",
    "Prepare this record for review",
)


def patient(pid=PID):
    return {
        "resourceType": "Patient",
        "id": pid,
        "meta": copy.deepcopy(META),
        "gender": "female",
        "birthDate": "1960-01-01",
    }


def report(*refs, rid="dra-report-1", pid=PID):
    return {
        "resourceType": "DiagnosticReport",
        "id": rid,
        "meta": copy.deepcopy(META),
        "status": "final",
        "code": {"text": "MOCK diagnostic panel"},
        "subject": {"reference": f"Patient/{pid}"},
        "effectiveDateTime": DATE,
        "result": [{"reference": ref} for ref in refs],
    }


def observation(oid="dra-obs-1", pid=PID, value=0):
    obs = {
        "resourceType": "Observation",
        "id": oid,
        "meta": copy.deepcopy(META),
        "status": "final",
        "code": {"text": "MOCK measured result"},
        "subject": {"reference": f"Patient/{pid}"},
        "effectiveDateTime": DATE,
    }
    obs["valueString" if isinstance(value, str) else "valueQuantity"] = (
        value if isinstance(value, str) else {"value": value, "unit": "mg/L"}
    )
    return obs


@pytest.fixture()
def records_for():
    databases = []

    def make(*resources):
        db = Database(":memory:")
        databases.append(db)
        store = FHIRStore(db)
        ingested = store.ingest([patient(), *resources], source="invented-MOCK")
        assert ingested.invalid == 0, ingested.as_dict()
        return ClinicalRecords(store)

    yield make
    for db in databases:
        db.close()


def unavailable(records, pid=PID):
    return [f for f in records.missing_information(pid) if f["category"] == CATEGORY]


def test_an_absent_result_is_a_report_cited_availability_limitation(records_for):
    raw = report("Observation/dra-absent")
    records = records_for(raw)
    (finding,) = unavailable(records)
    assert finding["severity"] == "medium"
    assert finding["source"] == source_of(raw)
    assert finding["source"]["version"] == "2"
    assert finding["unresolved_results"] == ["Observation/dra-absent"]
    assert finding["message"] == (
        f"Diagnostic report 'MOCK diagnostic panel' ({DATE}) lists 1 referenced result that cannot be resolved "
        "in this imported record. The referenced result cannot be reviewed or interpreted from this record."
    )
    assert META["lastUpdated"] not in finding["message"]
    assert records.store.get("DiagnosticReport", raw["id"]) == raw


def test_mixed_and_duplicate_references_give_one_finding_with_only_unresolved_results(records_for):
    records = records_for(
        observation(),
        report(
            "Observation/dra-obs-1", "Observation/dra-absent-b", "Observation/dra-absent-a", "Observation/dra-absent-b"
        ),
    )
    (finding,) = unavailable(records)
    assert finding["unresolved_results"] == ["Observation/dra-absent-b", "Observation/dra-absent-a"]
    assert "lists 2 referenced results" in finding["message"]
    assert "The referenced results cannot be reviewed or interpreted" in finding["message"]


def test_each_affected_report_has_its_own_finding_in_stable_report_order(records_for):
    records = records_for(
        report("Observation/missing", rid="dra-report-b"), report("Observation/missing", rid="dra-report-a")
    )
    assert [f["source"]["reference"] for f in unavailable(records)] == [
        "DiagnosticReport/dra-report-a",
        "DiagnosticReport/dra-report-b",
    ]


@pytest.mark.parametrize("value", [0, -1, "Negative", "Normal"])
def test_a_resolved_result_is_available_regardless_of_its_value(records_for, value):
    records = records_for(observation(value=value), report("Observation/dra-obs-1"))
    assert unavailable(records) == []


@pytest.mark.parametrize("refs", [(), (None,), ("",), (None, "")])
def test_a_report_without_a_result_reference_does_not_invent_one(records_for, refs):
    assert unavailable(records_for(report(*refs))) == []


@pytest.mark.parametrize(
    "ref",
    [
        "https://example.invalid/Observation/dra-obs-1",
        "Observation/dra-obs-1/_history/1",
        "#dra-obs-1",
        "urn:uuid:dra-obs-1",
        "Condition/dra-obs-1",
    ],
)
def test_noncanonical_references_are_not_resolved_by_guessing_a_local_id(records_for, ref):
    (finding,) = unavailable(records_for(observation(), report(ref)))
    assert finding["unresolved_results"] == [ref]
    assert "cannot be resolved in this imported record" in finding["message"]


def test_another_patients_observation_cannot_resolve_the_report(records_for):
    records = records_for(
        patient(OTHER), observation(pid=OTHER, value="FOREIGN RESULT TEXT"), report("Observation/dra-obs-1")
    )
    (finding,) = unavailable(records)
    assert finding["unresolved_results"] == ["Observation/dra-obs-1"]
    assert OTHER not in finding["message"] and "FOREIGN RESULT TEXT" not in finding["message"]
    assert unavailable(records, OTHER) == []


@pytest.mark.parametrize("coverage", [None, "not_imported", "returned_none", "unrecognised"])
def test_all_prior_findings_and_check_ids_stay_including_medication_and_encounter_gaps(runtime, coverage):
    pid = "synthetic_p003"
    runtime.records.source_coverage = {"MedicationRequest": coverage} if coverage else {}
    note = copy.deepcopy(runtime.store.all_for_patient(pid, "DocumentReference")[0])
    note["id"] = "dra-date-gap-note"
    note["context"] = {"encounter": [{"reference": f"Encounter/{runtime.records.encounters(pid)[0]['id']}"}]}
    note["content"][0]["attachment"]["data"] = base64.b64encode(b"Discharge date: 2025-11-01").decode()
    assert runtime.store.ingest([note], source="invented-MOCK").invalid == 0
    ctx = ToolContext(
        access=AccessContext(PERSONAS["clinician.demo"]),
        policy=runtime.policy,
        records=runtime.records,
        index=runtime.index,
        patient_id=pid,
    )
    before = run_tool(ctx, "detect_missing_information", {})
    assert before.ok and any(f["category"] == "encounter_documentation_incomplete" for f in before.data)
    assert runtime.store.ingest([report("Observation/dra-absent", pid=pid)], source="invented-MOCK").invalid == 0
    after = run_tool(ctx, "detect_missing_information", {})
    assert after.ok and after.data[-1]["category"] == CATEGORY
    assert after.data[:-1] == before.data
    assert after.evidence[:-1] == before.evidence
    assert after.scope == before.scope
    assert after.evidence[-1].source_ref == "DiagnosticReport/dra-report-1"
    assert after.evidence[-1].source_version == "2"


def test_the_synthetic_report_resolves_both_results_and_issue7_is_preserved(runtime):
    (rep,) = runtime.records.diagnostic_reports("synthetic_p001")
    assert len(rep["results"]) == 2 and unavailable(runtime.records, "synthetic_p001") == []
    findings = runtime.records.missing_information("synthetic_p003")
    (med,) = [f for f in findings if f["category"] == "medication_list_missing"]
    assert med["source"]["reference"] == "Patient/synthetic_p003" and "coverage" not in med


@pytest.fixture(scope="module")
def epic_unavailable(tmp_path_factory):
    patcher = pytest.MonkeyPatch()
    patcher.setattr(settings, "REPO_ROOT", tmp_path_factory.mktemp("diagnostic-availability-mock"))
    patcher.delenv("CLINICAL_TRACE_DB_PATH", raising=False)
    pid, resources, binaries = fixture_patient()
    raw = next(r for r in resources if r["resourceType"] == "DiagnosticReport")
    raw["result"].append({"reference": "Observation/eM22cInventedAbsentResult"})
    snapshot = workspace.write_snapshot(
        pid, resources, binaries, retrieved_at=RETRIEVED_AT, mode="MOCK", snapshot_id="diagnostic-availability-01"
    )
    rt = workspace.load_runtime(snapshot.path)
    yield snapshot, rt, snapshot.patient_ids[0]
    rt.close()
    patcher.undo()


@pytest.mark.parametrize("question", QUESTIONS)
def test_completeness_summary_and_review_disclose_only_availability_with_epic_provenance(epic_unavailable, question):
    snapshot, rt, pid = epic_unavailable
    (rep,) = rt.records.diagnostic_reports(pid)
    raw_before = rt.store.get("DiagnosticReport", rep["id"])
    lineage_before = source_provenance(raw_before)
    result = rt.agent.run(workspace.local_access(snapshot), pid, question)
    assert result["error"] is None and result["outcome"] == "answered"
    assert result["inference"]["mode"] == "deterministic"
    assert result["verification"]["status"] == "verified"
    assert result["verification"]["isolation_violations"] == 0
    assert result["answer"]["rejected_claims"] == []
    evidence = {e["evidence_id"]: e for e in result["evidence"]}
    (claim,) = [
        c for c in result["answer"]["claims"] if any(x.startswith(f"check:{CATEGORY}:") for x in c["citations"])
    ]
    assert "cannot be resolved in this imported record" in claim["text"]
    assert "cannot be reviewed or interpreted from this record" in claim["text"]
    assert rep["effective"] in claim["text"] and RETRIEVED_AT not in claim["text"]
    assert {evidence[c]["source_ref"] for c in claim["citations"]} == {rep["source"]["reference"]}
    assert {evidence[c]["source_version"] for c in claim["citations"]} == {rep["source"]["version"]}
    assert not any(term in claim["text"].lower() for term in ("normal", "negative", "not performed", "not returned"))
    counts = workspace.citation_provenance(rt, result)
    assert counts["resolved_to_epic_source"] == counts["citations"]
    assert rt.store.get("DiagnosticReport", rep["id"]) == raw_before
    assert source_provenance(rt.store.get("DiagnosticReport", rep["id"])) == lineage_before
    assert any("medication orders were not imported" in n for n in result["answer"]["uncertainty"])
