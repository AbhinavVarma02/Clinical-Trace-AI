"""ISSUE-6 direct diagnostic answers on invented records and MOCK Epic."""

from __future__ import annotations

from typing import Any

import pytest

from src.interop.epic import settings, workspace
from src.runtime import Runtime
from src.safety.policy import AccessContext, Persona
from tests.epic_support import RETRIEVED_AT, fixture_patient
from tests.test_diagnostic_result_availability import DATE, META, OTHER, PID, observation, patient, report
from tests.test_medication_evidence import note

QUESTION = "What did the stress test show?"
CONCLUSION = "Exercise stopped because of recorded leg fatigue."
BANNER = "*** SYNTHETIC TEST DATA - fabricated note seeded into the Epic sandbox."


def diagnostic_report(*refs: str, **kwargs: Any) -> dict[str, Any]:
    return {**report(*refs, **kwargs), "code": {"text": "Stress test"}}


def ask(runtime: Runtime, *resources: dict[str, Any], question: str = QUESTION) -> dict[str, Any]:
    notes = [note(f"banner-{i}", BANNER, "Progress Notes", DATE, pid=PID) for i in range(3)]
    imported = runtime.store.ingest([patient(), *notes, *resources], source="invented")
    assert imported.invalid == 0, imported.as_dict()
    access = AccessContext(Persona("issue6.clinician", "Invented clinician", "clinician", (PID,)))
    result = runtime.agent.run(access, PID, question)
    assert result["error"] is None
    assert result["verification"]["isolation_violations"] == 0
    assert all(item["patient_id"] == PID for item in result["evidence"])
    assert result["answer"]["rejected_claims"] == []
    return result


def cited(result: dict[str, Any], text: str) -> dict[str, Any]:
    assert result["outcome"] == "answered"
    assert result["verification"]["status"] == "verified"
    (claim,) = result["answer"]["claims"]
    assert text in claim["text"]
    (item,) = [e for e in result["evidence"] if e["evidence_id"] in claim["citations"]]
    return item


@pytest.mark.parametrize("question", [QUESTION, "What were the stress test results?", "Was the stress test normal?"])
def test_matched_unavailable_report_is_cited_instead_of_generic_notes(runtime, question):
    result = ask(
        runtime,
        diagnostic_report("Observation/dra-obs-1"),
        observation(value="FOREIGN NEGATIVE RESULT", pid=OTHER),
        patient(OTHER),
        report("Observation/other-panel-missing", rid="other-panel"),
        question=question,
    )
    item = cited(result, "cannot be reviewed or interpreted from this record")
    assert item["source_ref"] == "DiagnosticReport/dra-report-1" and item["source_version"] == "2"
    assert DATE in item["text"] and META["lastUpdated"] not in item["text"]
    assert "FOREIGN" not in str(result["answer"]) and "MOCK diagnostic panel" not in str(result["answer"])


def test_related_report_conclusion_is_quoted_verbatim(runtime):
    result = ask(
        runtime,
        {**diagnostic_report("Observation/dra-obs-1"), "conclusion": CONCLUSION},
        observation(),
        {**report(rid="other-panel"), "conclusion": "Unrelated panel conclusion."},
    )
    item = cited(result, f'"{CONCLUSION}"')
    assert item["source_ref"] == "DiagnosticReport/dra-report-1" and item["source_version"] == "2"
    assert item["date"] == DATE


@pytest.mark.parametrize(
    "question",
    [
        "What did the stress test show, and is the patient on an anticoagulant?",
        "Is the patient on an anticoagulant, and what did the stress test show?",
        "What did the stress test show? What medications are documented?",
        "What did the stress test show and how has their HbA1c changed?",
        "What did the stress test show; was the patient readmitted?",
        "What did the stress test show, and is warfarin documented?",
    ],
)
def test_diagnostic_and_separate_clinical_requests_abstain_before_evidence(runtime, question):
    result = ask(
        runtime,
        {**diagnostic_report(), "conclusion": CONCLUSION},
        note("anticoagulant", "CONTINUED: apixaban 5 mg twice daily.", "Progress Notes", DATE, pid=PID),
        question=question,
    )
    assert result["outcome"] == "abstained", result["answer"]
    assert result["answer"]["claims"] == []
    assert "separate questions" in result["answer"]["abstain_reason"]
    assert result["plan"]["calls"] == result["tool_calls"] == result["evidence"] == []


@pytest.mark.parametrize(
    "question",
    [
        "What did the stress test show while the patient was taking warfarin?",
        "What did the stress test on 2025-03-06 show while the patient was taking warfarin and aspirin?",
        "What did the stress test show while the patient was taking warfarin and was on apixaban?",
        "What did the stress test show, and was it normal while the patient was taking warfarin?",
        "When the patient was taking warfarin, what did the stress test show?",
        "What did the stress test show when the patient was taking warfarin and when the patient was taking apixaban?",
    ],
)
def test_contextual_medication_mentions_keep_the_diagnostic_answer(runtime, question):
    result = ask(runtime, {**diagnostic_report(), "conclusion": CONCLUSION}, question=question)
    item = cited(result, f'"{CONCLUSION}"')
    assert item["source_ref"] == "DiagnosticReport/dra-report-1" and item["source_version"] == "2"


def test_report_conclusion_does_not_hide_an_unavailable_result(runtime):
    result = ask(runtime, {**diagnostic_report("Observation/absent"), "conclusion": "Normal."})
    cited(result, "cannot be reviewed or interpreted from this record")
    assert "Normal" not in str(result["answer"])


def test_resolved_observation_without_a_conclusion_is_reported_as_recorded(runtime):
    result = ask(
        runtime,
        diagnostic_report("Observation/dra-obs-1"),
        observation(value=0),
        observation(oid="unrelated-result", value=77),
    )
    (claim,) = result["answer"]["claims"]
    assert "MOCK measured result: 0 mg/L" in claim["text"]
    assert "77" not in claim["text"] and "normal" not in claim["text"].lower()
    evidence = {e["evidence_id"]: e for e in result["evidence"]}
    assert {(evidence[c]["source_ref"], evidence[c]["source_version"]) for c in claim["citations"]} == {
        ("DiagnosticReport/dra-report-1", "2"),
        ("Observation/dra-obs-1", "2"),
    }


def test_result_reference_is_not_a_value_when_no_supported_value_was_recorded(runtime):
    resource = observation()
    del resource["valueQuantity"]
    result = ask(runtime, diagnostic_report("Observation/dra-obs-1"), resource)
    assert result["outcome"] == "abstained" and result["answer"]["claims"] == []


def test_repeated_banner_other_tests_and_a_scheduled_test_do_not_answer(runtime):
    result = ask(
        runtime,
        {**report("Observation/other-missing", rid="other-panel"), "conclusion": "Unrelated panel conclusion."},
        note(
            "scheduled",
            "The stress test was scheduled. The lab test showed a normal value.",
            "Progress Notes",
            DATE,
            pid=PID,
        ),
    )
    assert result["outcome"] == "abstained"
    assert result["answer"]["claims"] == []
    assert "documented result" in result["answer"]["abstain_reason"]


def test_a_note_only_result_is_quoted_without_inference(runtime):
    sentence = "The stress test showed an early stop for fatigue."
    result = ask(runtime, note("result-note", sentence, "Progress Notes", DATE, pid=PID))
    item = cited(result, f'"{sentence}"')
    assert item["source_ref"] == "DocumentReference/result-note"


def test_unqualified_question_does_not_choose_between_reports(runtime):
    result = ask(runtime, diagnostic_report("Observation/absent"), diagnostic_report(rid="second-report"))
    assert result["outcome"] == "abstained" and result["answer"]["claims"] == []
    assert "date" in result["answer"]["abstain_reason"]


def test_iso_date_selects_the_report_and_keeps_note_date_separate(runtime):
    second = {**diagnostic_report(rid="second-report"), "effectiveDateTime": "2025-03-14T10:30:00Z"}
    result = ask(
        runtime,
        diagnostic_report("Observation/absent"),
        {**second, "conclusion": CONCLUSION},
        question="What did the stress test on 2025-03-14 show?",
    )
    item = cited(result, f'"{CONCLUSION}"')
    assert item["source_ref"] == "DiagnosticReport/second-report" and "2025-03-14" in item["date"]


def test_focused_diagnostic_run_never_calls_configured_provider(runtime, monkeypatch):
    class UnexpectedProvider:
        def complete(self, *_args: Any, **_kwargs: Any) -> None:
            pytest.fail("The direct diagnostic path attempted language-model inference.")

    monkeypatch.setattr(runtime.agent, "provider", UnexpectedProvider())
    result = ask(runtime, diagnostic_report("Observation/absent"))
    cited(result, "cannot be reviewed or interpreted from this record")
    assert result["inference"]["mode"] == "deterministic"
    assert result["inference"]["provider"] is None


def test_mock_epic_direct_disclosure_retains_report_provenance(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "REPO_ROOT", tmp_path)
    pid, resources, binaries = fixture_patient()
    raw = next(resource for resource in resources if resource["resourceType"] == "DiagnosticReport")
    raw["code"] = {"text": "Stress test"}
    raw["result"].append({"reference": "Observation/eIssue6MissingResult"})
    snapshot = workspace.write_snapshot(
        pid, resources, binaries, retrieved_at=RETRIEVED_AT, mode="MOCK", snapshot_id="issue6-diagnostic"
    )
    runtime = workspace.load_runtime(snapshot.path)
    try:
        patient_id = snapshot.patient_ids[0]
        (rep,) = runtime.records.diagnostic_reports(patient_id)
        result = runtime.agent.run(workspace.local_access(snapshot), patient_id, QUESTION)
        item = cited(result, "cannot be reviewed or interpreted from this record")
        assert item["source_ref"] == rep["source"]["reference"] and item["source_version"] == rep["source"]["version"]
        counts = workspace.citation_provenance(runtime, result)
        assert counts["resolved_to_epic_source"] == counts["citations"] == 1
    finally:
        runtime.close()
