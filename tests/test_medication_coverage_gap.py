"""The "no medication orders" completeness finding under each declared import coverage (M2.2-b). Offline only.

No resource can prove an absence. The Patient resource says nothing about medication orders, so it is never cited
as evidence that none exist. What a data source declares about its import (its query/import provenance) is stated
as an uncited, labelled note, never as a cited claim:

* ``not_imported``: medication orders were not imported. No ``medication_list_missing`` finding is made at all;
  completeness answers carry the not-imported note.
* ``returned_none``: the source was searched and returned no orders. The record view keeps the finding as a
  warning, labelled with its coverage. Answers state it as the uncited "returned no medication orders" note instead
  of a Patient-cited gap claim.
* a declared value the application does not recognise: as ``returned_none``, with the notes "No medication orders
  are recorded in this record." and the conservative "absence does not show ..." caveat.
* nothing declared (the synthetic fixture): unchanged, including its Patient-cited gap claim (tracked separately
  as ISSUE-7; changing it would alter the approved synthetic demo answers).

The readmission eligibility note says "not imported" instead of "No medication orders are recorded" only for
``not_imported``. Other completeness findings are never affected. Synthetic p003 (no orders) and the MOCK
Epic-shaped fixture (declares ``not_imported``) are used; no private snapshot, Epic API or language model.
"""

from __future__ import annotations

import pytest

from src.agent.composer import MEDICATION_COVERAGE, MEDICATION_ORDERS_NONE
from src.agent.tools import ToolContext, run_tool
from src.clinical.risk_bridge import assess
from src.interop.epic import settings, workspace
from src.runtime import build_runtime
from src.safety.policy import PERSONAS, AccessContext
from tests.epic_support import RETRIEVED_AT, fixture_patient
from tests.test_epic_agent_compat import QUESTIONS as EPIC_QUESTIONS

P003 = "synthetic_p003"
NONE = "No medication orders are recorded in this record."
NOT_IMPORTED = (
    "Structured medication orders were not imported from the data source for this record, so the absence "
    "of orders here does not show that none exist at the source."
)
RETURNED_NONE = "The data source returned no medication orders for this patient when this record was imported."
UNDECLARED = "The absence of medication orders in this record does not show that the patient takes no medication."
ALL_NOTES = (NONE, NOT_IMPORTED, RETURNED_NONE, UNDECLARED)
UNRECOGNISED = "partially_imported"
# The uncited notes a completeness answer carries for each declared coverage (undeclared: none, as before).
EXPECTED_NOTES = {
    "not_imported": [NOT_IMPORTED],
    "returned_none": [RETURNED_NONE],
    UNRECOGNISED: [NONE, UNDECLARED],
    None: [],
}
GAP = "Documentation gap: No medication orders are recorded for this patient."
RISK_RECORDED = "No medication orders are recorded, so medication counts cannot be derived."
RISK_NOT_IMPORTED = (
    "Medication orders were not imported from the data source for this record, so medication counts cannot be derived."
)
COMPLETENESS_QUESTIONS = (
    "Is any documentation missing?",
    "Summarize this patient journey",
    "Prepare this record for review",
)
P003_CATEGORIES = [
    "encounter_end_missing",
    "discharge_summary_missing",
    "unit_missing",
    "medication_list_missing",
    "note_incomplete",
    "note_incomplete",
    "note_incomplete",
    "note_incomplete",
]


@pytest.fixture(scope="module")
def synthetic():
    runtime = build_runtime(":memory:", provider=None, enable_model=False)
    yield runtime
    runtime.close()


@pytest.fixture(scope="module")
def epic(tmp_path_factory):
    patcher = pytest.MonkeyPatch()
    patcher.setattr(settings, "REPO_ROOT", tmp_path_factory.mktemp("coverage-gap-repo"))
    patcher.delenv("CLINICAL_TRACE_DB_PATH", raising=False)
    pid, resources, binaries = fixture_patient()
    snapshot = workspace.write_snapshot(
        pid, resources, binaries, retrieved_at=RETRIEVED_AT, mode="MOCK", snapshot_id="coverage-gap-01"
    )
    runtime = workspace.load_runtime(snapshot.path)
    yield snapshot, runtime, snapshot.patient_ids[0]
    runtime.close()
    patcher.undo()


def declare(runtime, coverage: str | None) -> None:
    runtime.records.source_coverage = {"MedicationRequest": coverage} if coverage else {}


def ask(runtime, access, pid: str, question: str) -> dict:
    result = runtime.agent.run(access, pid, question)
    assert result["error"] is None and result["inference"]["mode"] == "deterministic"
    assert result["answer"]["rejected_claims"] == []
    assert all(e["patient_id"] == pid for e in result["evidence"])
    return result


def ask_synthetic(synthetic, coverage: str | None, question: str) -> dict:
    declare(synthetic, coverage)
    try:
        return ask(synthetic, AccessContext(PERSONAS["clinician.demo"]), P003, question)
    finally:
        declare(synthetic, None)


def patient_cited(result: dict) -> list[dict]:
    """Claims citing the Patient resource."""
    evidence = {e["evidence_id"]: e for e in result["evidence"]}
    return [
        c
        for c in result["answer"]["claims"]
        if any(evidence[x]["source_ref"].startswith("Patient/") for x in c["citations"])
    ]


def notes(result: dict) -> list[str]:
    return [n for n in result["answer"]["uncertainty"] if n in ALL_NOTES]


def test_the_notes_are_the_ones_medication_answers_already_use():
    assert MEDICATION_ORDERS_NONE == NONE
    assert MEDICATION_COVERAGE["not_imported"] == NOT_IMPORTED
    assert MEDICATION_COVERAGE["returned_none"] == RETURNED_NONE
    assert MEDICATION_COVERAGE["undeclared"] == UNDECLARED


# ----------------------------------------------------------------------------- completeness finding (record view)
def test_not_imported_coverage_drops_only_the_medication_finding(synthetic):
    declare(synthetic, "not_imported")
    try:
        categories = [f["category"] for f in synthetic.records.missing_information(P003)]
    finally:
        declare(synthetic, None)
    assert categories == [c for c in P003_CATEGORIES if c != "medication_list_missing"]


@pytest.mark.parametrize("coverage", ["returned_none", UNRECOGNISED, None])
def test_the_record_view_keeps_the_warning_labelled_with_its_coverage(synthetic, coverage):
    declare(synthetic, coverage)
    try:
        findings = synthetic.records.missing_information(P003)
    finally:
        declare(synthetic, None)
    assert [f["category"] for f in findings] == P003_CATEGORIES
    finding = next(f for f in findings if f["category"] == "medication_list_missing")
    assert finding.get("coverage") == coverage  # undeclared adds no coverage key, as before


def test_a_record_with_orders_has_no_medication_finding_under_any_coverage(synthetic):
    for coverage in ("not_imported", "returned_none", UNRECOGNISED, None):
        declare(synthetic, coverage)
        try:
            categories = {f["category"] for f in synthetic.records.missing_information("synthetic_p001")}
        finally:
            declare(synthetic, None)
        assert "medication_list_missing" not in categories


# ------------------------------------------------------------------------------------------------- tool level
@pytest.mark.parametrize("coverage", ["not_imported", "returned_none", UNRECOGNISED, None])
def test_declared_coverage_is_provenance_not_evidence(synthetic, coverage):
    declare(synthetic, coverage)
    try:
        ctx = ToolContext(
            access=AccessContext(PERSONAS["clinician.demo"]),
            policy=synthetic.policy,
            records=synthetic.records,
            index=synthetic.index,
            patient_id=P003,
            predictor=None,
        )
        result = run_tool(ctx, "detect_missing_information", {})
    finally:
        declare(synthetic, None)
    assert result.ok
    patient_evidence = [e.evidence_id for e in result.evidence if e.source_ref.startswith("Patient/")]
    categories = [f["category"] for f in result.data]
    if coverage:
        assert result.scope == {"medication_orders": "none_recorded", "coverage": coverage}
        assert patient_evidence == []
        assert categories == [c for c in P003_CATEGORIES if c != "medication_list_missing"]
    else:
        assert result.scope == {}
        assert patient_evidence == ["check:medication_list_missing:4"]  # unchanged wording, position and source
        assert categories == P003_CATEGORIES


# ----------------------------------------------------------------------------------- answers (synthetic p003)
@pytest.mark.parametrize("coverage", ["not_imported", "returned_none", UNRECOGNISED])
@pytest.mark.parametrize("question", COMPLETENESS_QUESTIONS)
def test_declared_coverage_is_stated_as_a_note_and_other_gaps_are_kept(synthetic, question, coverage):
    result = ask_synthetic(synthetic, coverage, question)
    assert result["outcome"] == "answered"
    assert [c["kind"] for c in patient_cited(result)] in ([], ["profile"])
    texts = [c["text"] for c in result["answer"]["claims"]]
    assert GAP not in texts and not any("medication orders" in t.lower() for t in texts)
    assert any(t.startswith("Documentation gap: Inpatient encounter") for t in texts)  # genuine gaps are kept
    assert notes(result) == EXPECTED_NOTES[coverage]


@pytest.mark.parametrize("question", COMPLETENESS_QUESTIONS)
def test_undeclared_coverage_is_unchanged(synthetic, question):
    """The synthetic demo keeps its Patient-cited gap claim and adds no note (ISSUE-7, tracked separately)."""
    result = ask_synthetic(synthetic, None, question)
    assert GAP in [c["text"] for c in result["answer"]["claims"]]
    assert notes(result) == []


# ----------------------------------------------------------------------------------- readmission eligibility
@pytest.mark.parametrize(
    ("coverage", "reason"),
    [
        ("not_imported", RISK_NOT_IMPORTED),
        ("returned_none", RISK_RECORDED),
        (UNRECOGNISED, RISK_RECORDED),
        (None, RISK_RECORDED),
    ],
)
def test_the_eligibility_note_refers_only_to_the_imported_record(synthetic, coverage, reason):
    declare(synthetic, coverage)
    try:
        reasons = assess(synthetic.records, P003)["reasons"]
    finally:
        declare(synthetic, None)
    assert reason in reasons
    assert [r for r in (RISK_RECORDED, RISK_NOT_IMPORTED) if r in reasons] == [reason]


# --------------------------------------------------------------------------------------- answers (MOCK Epic)
def test_the_epic_fixture_declares_not_imported_and_has_no_medication_finding(epic):
    snapshot, runtime, pid = epic
    assert runtime.records.source_coverage == {"MedicationRequest": "not_imported"}
    assert any("MedicationRequest" in note and "A1" in note for note in snapshot.manifest["notes"])
    assert "medication_list_missing" not in {f["category"] for f in runtime.records.missing_information(pid)}


@pytest.mark.parametrize("question", [*EPIC_QUESTIONS, "Prepare this record for review"])
def test_no_epic_answer_cites_the_patient_except_the_profile(epic, question):
    snapshot, runtime, pid = epic
    result = ask(runtime, workspace.local_access(snapshot), pid, question)
    assert [c["kind"] for c in patient_cited(result)] in ([], ["profile"])
    assert GAP not in [c["text"] for c in result["answer"]["claims"]]
    assert not any(RISK_RECORDED in n for n in result["answer"]["uncertainty"])
    provenance = workspace.citation_provenance(runtime, result)
    assert provenance.get("unresolved", 0) == 0 and provenance.get("resolved_without_epic_provenance", 0) == 0


@pytest.mark.parametrize("question", ["Summarize this patient's hospital course", "Prepare this record for review"])
def test_epic_summary_and_review_answers_carry_the_scope_note(epic, question):
    snapshot, runtime, pid = epic
    result = ask(runtime, workspace.local_access(snapshot), pid, question)
    assert result["outcome"] == "answered"
    assert NOT_IMPORTED in result["answer"]["uncertainty"]


def test_the_epic_review_answer_says_orders_were_not_imported(epic):
    snapshot, runtime, pid = epic
    result = ask(runtime, workspace.local_access(snapshot), pid, "Prepare this record for review")
    assert (
        f"The readmission model is not applicable to this record: {RISK_NOT_IMPORTED}"
        in result["answer"]["uncertainty"]
    )


@pytest.mark.parametrize(
    ("coverage", "reason"),
    [
        (
            "not_imported",
            "The completeness checks reported no documentation gaps for the imported record. " + NOT_IMPORTED,
        ),
        (
            "returned_none",
            "The completeness checks reported no other documentation gaps for the imported record. " + RETURNED_NONE,
        ),
        (
            UNRECOGNISED,
            f"The completeness checks reported no other documentation gaps for the imported record. {NONE} {UNDECLARED}",
        ),
    ],
)
def test_an_epic_completeness_answer_with_no_other_gap_says_what_was_checked(epic, coverage, reason):
    snapshot, runtime, pid = epic
    declare(runtime, coverage)
    try:
        result = ask(runtime, workspace.local_access(snapshot), pid, "Is any documentation missing?")
    finally:
        declare(runtime, "not_imported")
    assert result["outcome"] == "abstained"
    assert result["answer"]["claims"] == []
    assert result["answer"]["abstain_reason"] == reason
    assert notes(result) == EXPECTED_NOTES[coverage]
