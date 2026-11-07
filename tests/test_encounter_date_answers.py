"""M2.2-e: existing linked-date findings in encounter/summary answers; invented/MOCK data only."""

from __future__ import annotations

import copy
from typing import Any, cast

import pytest

from src.agent import composer
from src.agent.planner import deterministic_plan
from src.agent.tools import ToolContext, run_tool
from src.safety.verifier import verify_claims
from tests import test_encounter_date_consistency as dates
from tests import test_epic_encounter_mismatch as epic

QUESTIONS = (
    "When was the patient admitted?",
    "Show the encounters.",
    "Summarize this patient journey.",
    "Summarize the admissions and missing documentation.",
    "What follow-up was planned at discharge, and is anything missing?",
    "Prepare a summary for review with any inconsistencies.",
)


@pytest.fixture(scope="module")
def date_runtime(tmp_path_factory):
    note = copy.deepcopy(dates.note())
    note["meta"]["versionId"] = "3"
    encounter = copy.deepcopy(dates.encounter())
    encounter["meta"]["versionId"] = "2"
    # Another patient's date text points at this encounter; it must never enter this patient's answer.
    other = dates.note("e5-other-note", text="Admission date: 2031-04-09", pid=dates.OTHER)
    runtime = dates._runtime(
        tmp_path_factory.mktemp("e5"), [dates.patient(), dates.patient(dates.OTHER), encounter, note, other]
    )
    assert runtime.startup_ingestion["invalid"] == 0
    yield runtime
    runtime.close()


def _tool(runtime, pid=dates.PID):
    ctx = ToolContext(
        access=dates._access(pid),
        policy=runtime.policy,
        records=runtime.records,
        index=runtime.index,
        patient_id=pid,
        predictor=None,
    )
    result = run_tool(ctx, "get_patient_encounters", {})
    assert result.ok
    return result


def _conflicts(result):
    return [
        claim
        for claim in result["answer"]["claims"]
        if claim["kind"] == "inconsistency" and "may conflict" in claim["text"]
    ]


def _assert_answer(result, pid):
    assert result["error"] is None and result["outcome"] == "answered"
    assert result["inference"]["mode"] == "deterministic"
    assert result["verification"]["status"] == "verified"
    assert result["verification"]["isolation_violations"] == 0
    assert result["answer"]["rejected_claims"] == []
    assert all(item["patient_id"] == pid for item in result["evidence"])


def test_encounter_tool_exposes_the_existing_finding_without_mutating_records(date_runtime):
    before = copy.deepcopy(date_runtime.records.encounters(dates.PID))
    tool = _tool(date_runtime)
    findings = [finding for encounter in tool.data for finding in encounter.get("date_conflicts", [])]
    assert len(findings) == 1 and findings[0]["category"] == "encounter_date_conflict"
    assert "admission date 2026-08-05" in findings[0]["message"]
    assert date_runtime.records.encounters(dates.PID) == before
    assert {item.source_ref for item in tool.evidence} >= {dates.DS, dates.ENC}
    assert {item.source_version for item in tool.evidence if item.source_ref == dates.DS} == {"3"}


@pytest.mark.parametrize("question", QUESTIONS)
def test_encounter_and_summary_answers_disclose_all_three_date_roles_once(date_runtime, question):
    result = date_runtime.agent.run(dates._access(dates.PID), dates.PID, question)
    _assert_answer(result, dates.PID)
    conflicts = _conflicts(result)
    assert len(conflicts) == 1
    claim = conflicts[0]
    assert "admission date 2026-08-05" in claim["text"]
    assert "start 2019-05-28T15:22:00Z" in claim["text"]
    assert "both values are shown as recorded" in claim["text"]
    assert "2026-08-07" not in claim["text"]  # missing end remains a documentation gap
    evidence = {item["evidence_id"]: item for item in result["evidence"]}
    assert [(evidence[c]["source_ref"], evidence[c]["source_version"]) for c in claim["citations"]] == [
        (dates.DS, "3"),
        (dates.DS, "3"),
        (dates.ENC, "2"),
    ]
    text = " ".join(c["text"] for c in result["answer"]["claims"])
    assert "note date 2026-11-02" in text and "2031-04-09" not in text
    assert result["review"]["required"]
    assert result["review"]["reasons"].count("the record contains inconsistencies") == 1
    assert len({(c["kind"], c["text"], tuple(c["citations"])) for c in result["answer"]["claims"]}) == len(
        result["answer"]["claims"]
    )


@pytest.mark.parametrize("question", (QUESTIONS[0], QUESTIONS[2]))
def test_another_patient_cannot_borrow_the_encounter_or_its_date_finding(date_runtime, question):
    result = date_runtime.agent.run(dates._access(dates.OTHER), dates.OTHER, question)
    assert _conflicts(result) == []
    assert all(item["patient_id"] == dates.OTHER for item in result["evidence"])
    assert dates.ENC not in {item["source_ref"] for item in result["evidence"]}


@pytest.mark.parametrize(
    "text,start,end,link",
    (
        ("Admission date: 2026-08-05", "2026-08-05T14:00:00Z", None, True),
        ("Admission date: 2026-08-06", "2026-08-05T23:55:00Z", None, True),
        ("Written on 2026-11-02; reviewed old encounter.", "2019-05-28T15:22:00Z", None, True),
        ("Admission date: 2026-02-30", "2019-05-28T15:22:00Z", None, True),
        ("Admitted on August 5, 2026.", "2019-05-28T15:22:00Z", None, True),
        ("Admission date: 2026-08-05", None, None, True),
        ("Discharge date: 2026-08-07", "2019-05-28T15:22:00Z", None, True),
        ("Admission date: 2026-08-05", "2019-05-28T15:22:00Z", None, False),
    ),
)
def test_date_difference_alone_or_missing_counterpart_is_not_a_conflict(tmp_path, text, start, end, link):
    runtime = dates._runtime(
        tmp_path,
        [
            dates.patient(),
            dates.encounter(start=start, end=end),
            dates.note(text=text, encounter_ref=dates.ENC if link else None),
        ],
    )
    try:
        tool = _tool(runtime)
        assert not any(item.get("date_conflicts") for item in tool.data)
        plan = deterministic_plan(QUESTIONS[0])
        draft = composer.compose_deterministic(QUESTIONS[0], plan.intents, {tool.tool: tool})
        assert not any(claim["kind"] == "inconsistency" for claim in draft.claims)
    finally:
        runtime.close()


def test_two_notes_and_an_unrelated_smoking_conflict_keep_distinct_check_identities(tmp_path):
    resources = [
        dates.patient(),
        dates.encounter(),
        dates.note(text="Admission date: 2026-08-05. Current smoker."),
        dates.note("e5-second", text="Admission date: 2026-08-09. Former smoker."),
    ]
    runtime = dates._runtime(tmp_path, resources)
    try:
        result = runtime.agent.run(dates._access(dates.PID), dates.PID, QUESTIONS[-1])
        _assert_answer(result, dates.PID)
        assert len(_conflicts(result)) == 2
        assert len({claim["citations"][0] for claim in _conflicts(result)}) == 2
        assert (
            len([claim for claim in result["answer"]["claims"] if "Smoking status is documented" in claim["text"]]) == 1
        )
        assert result["review"]["reasons"].count("the record contains inconsistencies") == 1
    finally:
        runtime.close()


def test_date_claim_with_a_stale_source_version_is_rejected(date_runtime):
    tool = _tool(date_runtime)
    plan = deterministic_plan(QUESTIONS[0])
    draft = composer.compose_deterministic(QUESTIONS[0], plan.intents, {tool.tool: tool})
    conflicts = [claim for claim in draft.claims if claim["kind"] == "inconsistency"]
    assert conflicts
    evidence = {item.evidence_id: item for item in tool.evidence}
    assert verify_claims(conflicts, evidence, dates.PID)["status"] == "verified"
    tampered = copy.deepcopy(conflicts)
    tampered[0]["citations"][1] = "edc-doc-ds@v1"
    assert verify_claims(tampered, evidence, dates.PID)["rejected"]


def test_discharge_conflict_does_not_mislabel_the_matching_admission(tmp_path):
    runtime = dates._runtime(
        tmp_path,
        [
            dates.patient(),
            dates.encounter(start="2026-08-05T14:00:00Z", end="2026-08-07T16:00:00Z", status="finished"),
            dates.note(text="Admission date: 2026-08-05. Discharge date: 2026-08-12."),
        ],
    )
    try:
        result = runtime.agent.run(dates._access(dates.PID), dates.PID, QUESTIONS[2])
        _assert_answer(result, dates.PID)
        assert len(_conflicts(result)) == 1
        text = _conflicts(result)[0]["text"]
        assert "discharge date 2026-08-12" in text and "end 2026-08-07T16:00:00Z" in text
        assert "admission date" not in text
    finally:
        runtime.close()


def test_separate_source_runtime_cannot_supply_a_missing_conflict(date_runtime, tmp_path):
    runtime = dates._runtime(
        tmp_path, [dates.patient(), dates.encounter(), dates.note(text="Follow-up plan: clinic visit in one week.")]
    )
    try:
        conflict = date_runtime.agent.run(dates._access(dates.PID), dates.PID, QUESTIONS[2])
        separate = runtime.agent.run(dates._access(dates.PID), dates.PID, QUESTIONS[2])
        assert len(_conflicts(conflict)) == 1 and _conflicts(separate) == []
        assert "the record contains inconsistencies" not in separate["review"]["reasons"]
        assert not any(item["source_version"] == "3" for item in separate["evidence"])
    finally:
        runtime.close()


@pytest.fixture(scope="module")
def mock_mismatch(tmp_path_factory):
    fixture = cast(Any, epic.mismatch).__wrapped__(tmp_path_factory)
    try:
        yield next(fixture)
    finally:
        fixture.close()


@pytest.mark.parametrize("question", QUESTIONS)
def test_mock_epic_encounter_and_summary_answers_keep_source_provenance(mock_mismatch, question):
    snapshot, runtime, pid = mock_mismatch
    result = epic._run(snapshot, runtime, pid, question)
    conflicts = _conflicts(result)
    assert len(conflicts) == 1
    assert epic._sources_of(runtime, result, conflicts[0]) == [epic.DS_SOURCE, epic.DS_SOURCE, epic.ENC_SOURCE]
    text = " ".join(claim["text"] for claim in result["answer"]["claims"])
    assert "note date 2026-11-02" in text
    assert "start 2019-05-28T15:22:00Z" in conflicts[0]["text"]
    assert result["review"]["reasons"].count("the record contains inconsistencies") == 1
