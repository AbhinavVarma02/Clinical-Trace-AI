"""Existing clinical workflow on an isolated Epic-shaped snapshot (MOCK/offline, deterministic, 0 LLM calls).

The unchanged records layer, retrieval index, eleven tools, LangGraph agent, claim
verifier and human review run over a snapshot built from the hand-authored
Epic-shaped fixture. This proves contract compatibility only; it is not evidence of
live Epic access (see the LIVE gates in docs/EPIC_SANDBOX.md).
"""

from __future__ import annotations

import re

import pytest

from src.agent.tools import TOOLS, ToolContext, run_tool
from src.clinical.fhir_models import parse_timestamp
from src.interop.epic import mapping, settings, workspace
from src.retrieval.chunking import chunk_patient
from tests.epic_support import RETRIEVED_AT, fixture_patient

QUESTIONS = (
    "Summarize this patient's hospital course",
    "How have the lab results trended?",
    "What happened at discharge and at the follow-up visit?",
    "Is any documentation missing?",
    "Are there any inconsistencies in the record?",
    "What medications are documented?",
)
_REF = re.compile(r"^([A-Z][A-Za-z]+)/([A-Za-z0-9_.\-]+)$")


@pytest.fixture(scope="module")
def epic(tmp_path_factory):
    patcher = pytest.MonkeyPatch()
    repo = tmp_path_factory.mktemp("epic-compat-repo")
    patcher.setattr(settings, "REPO_ROOT", repo)
    patcher.delenv("CLINICAL_TRACE_DB_PATH", raising=False)
    pid, resources, binaries = fixture_patient()
    snapshot = workspace.write_snapshot(
        pid, resources, binaries, retrieved_at=RETRIEVED_AT, mode="MOCK", snapshot_id="compat-snapshot-01"
    )
    runtime = workspace.load_runtime(snapshot.path)
    yield snapshot, runtime, snapshot.patient_ids[0]
    runtime.close()
    patcher.undo()


def _resolves_to_epic(runtime, reference: str) -> bool:
    match = _REF.fullmatch(reference)
    resource = runtime.store.get(match.group(1), match.group(2)) if match else None
    return (
        bool(resource)
        and resource["meta"]["source"].startswith(settings.FHIR_BASE + "/")
        and any(e["url"] == mapping.PROVENANCE_URL for e in resource.get("extension") or [])
    )


def test_all_eleven_tools_run_and_their_evidence_resolves_to_epic_sources(epic):
    snapshot, runtime, pid = epic
    ctx = ToolContext(
        access=workspace.local_access(snapshot),
        policy=runtime.policy,
        records=runtime.records,
        index=runtime.index,
        patient_id=pid,
        predictor=None,
    )
    assert len(TOOLS) == 11
    for name in TOOLS:
        args = {"query": "pneumonia antibiotics CRP"} if name == "retrieve_clinical_notes" else {}
        result = run_tool(ctx, name, args)
        assert result.ok, (name, result.error_type)
        for item in result.evidence:
            assert item.patient_id == pid
            if _REF.fullmatch(item.source_ref):
                assert _resolves_to_epic(runtime, item.source_ref), (name, item.source_ref)
    risk = run_tool(ctx, "get_readmission_prediction", {})
    assert risk.data["available"] is False and risk.evidence == []


@pytest.mark.parametrize("question", QUESTIONS)
def test_unchanged_agent_answers_with_verified_epic_citations(epic, question):
    snapshot, runtime, pid = epic
    result = runtime.agent.run(workspace.local_access(snapshot), pid, question)
    assert result["error"] is None and result["outcome"] in ("answered", "abstained")
    assert result["inference"]["mode"] == "deterministic"
    if result["verification"]:
        assert result["verification"]["isolation_violations"] == 0
    provenance = workspace.citation_provenance(runtime, result)
    assert provenance.get("unresolved", 0) == 0
    assert provenance.get("resolved_without_epic_provenance", 0) == 0
    assert result["risk"] is None or not result["risk"].get("available")
    summary = workspace.review_summary(result)
    assert all(call["error_type"] is None for call in summary["tool_calls"]), summary["tool_calls"]


def test_summary_question_is_answered_with_citations(epic):
    snapshot, runtime, pid = epic
    result = runtime.agent.run(workspace.local_access(snapshot), pid, QUESTIONS[0])
    assert result["outcome"] == "answered"
    provenance = workspace.citation_provenance(runtime, result)
    assert provenance["citations"] > 0 and provenance["resolved_to_epic_source"] == provenance["citations"]


def test_retrieval_timeline_and_evidence_dates_never_come_from_retrieval_time(epic):
    _snapshot, runtime, pid = epic
    retrieved = parse_timestamp(RETRIEVED_AT)
    chunks = chunk_patient(runtime.records, pid)
    assert chunks and all(c.patient_id == pid for c in chunks)
    for chunk in chunks:
        assert not chunk.date or parse_timestamp(chunk.date) != retrieved, chunk.chunk_id
        assert _resolves_to_epic(runtime, chunk.source_ref)
    for event in runtime.records.timeline(pid):
        assert event["when"] is None or parse_timestamp(event["when"]) != retrieved
    hits = runtime.index.search(pid, "CRP fell after antibiotics", k=5)["results"]
    assert hits and all(hit["patient_id"] == pid and _resolves_to_epic(runtime, hit["source_ref"]) for hit in hits)


def test_snapshot_holds_only_its_own_patient(epic):
    _snapshot, runtime, pid = epic
    assert runtime.store.patient_ids() == [pid]
    assert runtime.agent._other_patient_markers(pid) == ()


def test_human_review_binds_approval_to_the_epic_artifact(epic):
    snapshot, runtime, pid = epic
    clinician = workspace.local_access(snapshot, "clinician")
    reviewer = workspace.local_access(snapshot, "reviewer")
    run = runtime.agent.run(clinician, pid, QUESTIONS[0])
    review = runtime.reviews.submit_run(clinician, run)
    decided = runtime.reviews.decide(reviewer, review["review_id"], "approved", review["artifact_hash"], None)
    assert decided["status"] == "approved" and decided["approval_valid"] is True
    assert decided["artifact"]["content"]["patient_id"] == pid


def test_unimported_medications_are_a_declared_scope_not_a_finding(epic):
    """Medications are not imported until A1 is approved; the manifest says so, and no gap is claimed (M2.2-b)."""
    snapshot, runtime, pid = epic
    categories = {f["category"] for f in runtime.records.missing_information(pid)}
    assert "medication_list_missing" not in categories
    assert runtime.records.source_coverage == {"MedicationRequest": "not_imported"}
    assert any("MedicationRequest" in note and "A1" in note for note in snapshot.manifest["notes"])


def test_summary_answer_repeats_no_known_false_finding(epic):
    """The two former false findings are fixed: the Epic-style "Discharge Summary" is recognised (P2), and the
    unimported medication orders are stated as an uncited import scope, not a Patient-cited gap (M2.2-b)."""
    snapshot, runtime, pid = epic
    result = runtime.agent.run(workspace.local_access(snapshot), pid, QUESTIONS[0])
    cited = {c for claim in result["answer"]["claims"] for c in claim["citations"]}
    assert not any(c.startswith(("check:discharge_summary_missing", "check:medication_list_missing")) for c in cited)
    assert not any(claim["kind"] == "missing_information" for claim in result["answer"]["claims"])
    assert any(note.startswith("Structured medication orders were not imported")
               for note in result["answer"]["uncertainty"])


def test_discharge_summary_written_in_epic_style_is_recognised(epic):
    """P2: the MOCK note's type is text "Discharge Summary" with LOINC 18842-5 (display "Discharge summary")."""
    _snapshot, runtime, pid = epic
    documents = runtime.records.documents(pid)
    assert any(d["type"] == "Discharge Summary" for d in documents)  # the Epic-style label is kept as written
    categories = {f["category"] for f in runtime.records.missing_information(pid)}
    assert "discharge_summary_missing" not in categories
