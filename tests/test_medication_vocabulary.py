"""M2.2-d approval gate: invented records and MOCK Epic only; no live services.

Keep intent, named-drug identity and bounded class matching separate. Retrieval
tests use the real index, not injected search hits. M2.2-f preserves the raw
retrieval reproducer and requires the agent to recover the supported passage.
"""

from __future__ import annotations

import json
import uuid

import pytest

from src.agent import composer
from src.agent.planner import detect_intents, deterministic_plan
from src.agent.tools import ToolResult
from src.interop.epic import settings, workspace
from src.interop.epic.client import BinaryContent
from src.runtime import build_runtime
from src.safety.policy import AccessContext, Persona
from src.safety.verifier import EvidenceItem
from tests.epic_support import RETRIEVED_AT, fixture_patient
from tests.test_epic_medication_answer import _note
from tests.test_medication_evidence import CAVEAT, COVERAGE, NONE, note, patient

CLASS_TERMS = (
    "anticoagulant",
    "anticoagulants",
    "anticoagulation",
    "anticoagulated",
    "blood thinner",
    "blood thinners",
    "blood-thinner",
    "blood-thinners",
)
DATE = "2026-09-01T09:00:00Z"


@pytest.mark.parametrize("term", ("apixaban", "torsemide", *CLASS_TERMS))
def test_vocabulary_routes_to_existing_medication_tools_without_rewriting_query(term):
    question = f"Is the patient on {term.upper()}?"
    plan = deterministic_plan(question)
    assert plan.intents == ["medications"]
    assert plan.calls == [
        {"name": "get_medication_history", "args": {}},
        {"name": "retrieve_clinical_notes", "args": {"query": question, "k": 5}},
    ]


@pytest.mark.parametrize("term", CLASS_TERMS)
def test_class_terms_are_never_named_drugs(term):
    assert composer._named_drugs(f"Is the patient on {term}?") == []


@pytest.mark.parametrize("drug", ("apixaban", "torsemide"))
def test_named_drug_identity_is_available_even_when_a_generic_dose_word_routes_the_question(drug):
    assert composer._named_drugs(f"What dose of {drug.upper()} is documented?") == [drug]


@pytest.mark.parametrize(
    "question",
    (
        "What is the blood pressure?",
        "How has INR trended?",
        "Summarize the hospital course",
        "What does AC mean?",
        "What is DOAC?",
        "Was apixabanase mentioned?",
        "Was torsemide123 mentioned?",
        "Was Eliquis mentioned?",
        "Was rivaroxaban mentioned?",
        "Was torasemide mentioned?",
    ),
)
def test_unrelated_terms_and_deferred_vocabulary_do_not_add_medication_intent(question):
    assert "medications" not in detect_intents(question)


def _draft(question, passage=None, coverage="not_imported", bm25=1.0):
    results = {
        "get_medication_history": ToolResult(
            tool="get_medication_history",
            ok=True,
            data=[],
            scope={"medication_orders": "none_recorded", "coverage": coverage},
        )
    }
    if passage is not None:
        item = EvidenceItem(
            evidence_id="vocab-note#s0@v1",
            patient_id="synthetic_vocab",
            source_ref="DocumentReference/vocab-note",
            source_version="1",
            text=passage,
            kind="retrieved",
            title="Progress Notes",
            date=DATE,
            scores={"bm25": bm25, "dense": 0.5},
        )
        results["retrieve_clinical_notes"] = ToolResult(
            tool="retrieve_clinical_notes",
            ok=True,
            evidence=[item],
        )
    return composer.compose_deterministic(question, detect_intents(question), results)


@pytest.mark.parametrize("coverage", ("not_imported", "returned_none", "undeclared", "unrecognized"))
@pytest.mark.parametrize("drug", ("apixaban", "torsemide"))
def test_absent_named_drug_abstains_and_preserves_coverage(coverage, drug):
    draft = _draft(f"Is the patient on {drug}?", "Metformin was continued.", coverage)
    expected = COVERAGE.get(coverage, COVERAGE[None])
    assert draft.claims == [] and draft.abstain_reason
    assert drug in draft.abstain_reason
    assert expected in " ".join([draft.abstain_reason, *draft.uncertainty]) or (
        composer.MEDICATION_ABSTAIN_TAIL.get(coverage, composer.MEDICATION_ABSTAIN_TAIL["undeclared"])
        in draft.abstain_reason
    )


@pytest.mark.parametrize("drug", ("apixaban", "torsemide"))
@pytest.mark.parametrize(
    "entry, question_event",
    (
        ("{drug} was not stopped.", "stopped"),
        ("Do not stop {drug}.", "stopped"),
        ("Consider stopping {drug}.", "stopped"),
        ("If {drug} is stopped, review the record.", "stopped"),
        ("Plan: start {drug} if needed.", "started"),
        ("Discharge instructions: Stop {drug}.", "stopped"),
    ),
)
def test_new_names_preserve_negated_planned_conditional_and_imperative_safeguards(drug, entry, question_event):
    draft = _draft(f"Was {drug} {question_event}?", entry.format(drug=drug))
    assert draft.claims == [] and draft.abstain_reason
    assert not any(f"documents that {drug} was" in value for value in draft.uncertainty)


@pytest.mark.parametrize("drug", ("apixaban", "torsemide"))
@pytest.mark.parametrize("event", ("stopped", "started"))
def test_explicit_events_are_quoted_without_inventing_a_reason_or_current_use(drug, event):
    passage = f"{drug.capitalize()} was {event}."
    draft = _draft(f"Why was {drug} {event}?", passage)
    assert any(passage in claim["text"] for claim in draft.claims)
    assert draft.abstain_reason is None
    assert NONE in draft.uncertainty and CAVEAT in draft.uncertainty
    assert any("does not itself state why" in value for value in draft.uncertainty)


@pytest.mark.parametrize(
    "passage, drug",
    (
        ("Warfarin was stopped, apixaban continued.", "apixaban"),
        ("Furosemide stopped and torsemide started.", "torsemide"),
        ("Apixaban continued, warfarin was stopped.", "apixaban"),
    ),
)
def test_another_drugs_stop_never_establishes_the_asked_drugs_stop(passage, drug):
    assert composer._entries(passage, drug, ["stopped"]) == []
    draft = _draft(f"Why was {drug} stopped?", passage)
    assert draft.claims == [] and draft.abstain_reason
    assert not any(f"documents that {drug} was stopped" in value for value in draft.uncertainty)


@pytest.mark.parametrize(
    "passage, drug",
    (
        ("Metformin 1000 mg was continued, apixaban was continued.", "apixaban"),
        ("Furosemide 40 mg stopped and torsemide started.", "torsemide"),
        ("Apixaban continued and metformin 1000 mg continued.", "apixaban"),
    ),
)
def test_another_drugs_dose_never_establishes_the_asked_drugs_dose(passage, drug):
    assert composer._entries(passage, drug, [], dose=True) == []
    draft = _draft(f"What dose of {drug} is documented?", passage)
    assert draft.claims == [] and draft.abstain_reason


def test_an_unambiguous_list_preserves_the_documented_dose():
    passage = "STOPPED: furosemide 40 mg daily - STARTED: torsemide 20 mg daily."
    draft = _draft("What dose of torsemide is documented?", passage)
    assert draft.claims and CAVEAT in draft.uncertainty
    assert all(claim["kind"] == "quote" and passage in claim["text"] for claim in draft.claims)
    assert not any("20 mg" in value or "40 mg" in value for value in draft.uncertainty)


@pytest.mark.parametrize("term", CLASS_TERMS)
def test_class_matching_uses_exact_names_in_returned_zero_bm25_passages_only(term):
    passage = "CONTINUED: apixaban 5 mg twice daily."
    draft = _draft(f"Is the patient on {term}?", passage, bm25=0.0)
    assert any(passage in claim["text"] for claim in draft.claims)
    assert CAVEAT in draft.uncertainty
    scope = " ".join(draft.uncertainty).lower()
    assert "warfarin" in scope and "apixaban" in scope and "not exhaustive" in scope
    if term.startswith("blood"):
        assert "ambiguous" in scope
    assert all(claim["kind"] == "quote" for claim in draft.claims)


@pytest.mark.parametrize("term", CLASS_TERMS)
def test_an_empty_class_search_is_never_reported_as_clinical_absence(term):
    draft = _draft(f"Is the patient on {term}?")
    assert draft.claims == [] and draft.abstain_reason
    scope = " ".join([draft.abstain_reason, *draft.uncertainty]).lower()
    assert "not exhaustive" in scope and "not imported" in scope
    assert "takes no" not in scope and "is not on" not in scope


@pytest.fixture(scope="module")
def vocab_runtime(tmp_path_factory):
    cases = {
        "only": [("only-apixaban", "CONTINUED: apixaban 5 mg twice daily.")],
        "conflict": [
            ("conflict-stop", "STOPPED: apixaban 5 mg twice daily."),
            ("conflict-restart", "Apixaban was restarted at 5 mg twice daily."),
        ],
        "other": [("other-torsemide", "STARTED: torsemide 40 mg daily.")],
        "crowded": [
            (f"clinic-{i}", "Anticoagulation clinic appointment was discussed; no medication list was provided.")
            for i in range(6)
        ]
        + [("crowded-apixaban", "CONTINUED: apixaban 5 mg twice daily.")],
    }
    resources = []
    for key, notes in cases.items():
        pid = f"synthetic_l4{key}"
        resources.append(patient(pid))
        resources.extend(note(did, text, "Progress Notes", DATE, pid=pid) for did, text in notes)
    path = tmp_path_factory.mktemp("l4-vocabulary") / "bundle.json"
    path.write_text(
        json.dumps(
            {"resourceType": "Bundle", "type": "collection", "entry": [{"resource": item} for item in resources]}
        ),
        encoding="utf-8",
    )
    runtime = build_runtime(":memory:", fixture=path, provider=None, enable_model=False)
    runtime.records.source_coverage = {"MedicationRequest": "not_imported"}
    yield runtime
    runtime.close()


def _ask(runtime, pid, question):
    access = AccessContext(
        Persona("l4.clinician", "Invented clinician", "clinician", (pid,)), request_id=str(uuid.uuid4())
    )
    result = runtime.agent.run(access, pid, question)
    assert result["error"] is None and result["inference"]["mode"] == "deterministic"
    assert all(item["patient_id"] == pid for item in result["evidence"])
    assert result["answer"]["rejected_claims"] == []
    return result


def test_actual_index_returns_an_apixaban_only_passage_with_zero_bm25(vocab_runtime):
    pid = "synthetic_l4only"
    hits = vocab_runtime.index.search(pid, "Is the patient on an anticoagulant?", k=5)["results"]
    matches = [hit for hit in hits if "apixaban" in hit["text"].lower()]
    assert len(matches) == 1 and matches[0]["scores"]["bm25"] == 0
    assert all(hit["patient_id"] == pid for hit in hits)


def test_end_to_end_class_answer_quotes_the_returned_apixaban_only_passage(vocab_runtime):
    result = _ask(vocab_runtime, "synthetic_l4only", "Is the patient on an anticoagulant?")
    assert result["outcome"] == "answered"
    assert any("CONTINUED: apixaban" in claim["text"] for claim in result["answer"]["claims"])
    assert CAVEAT in result["answer"]["uncertainty"]


def test_class_retrieval_gate_finds_apixaban_among_administrative_class_mentions(vocab_runtime):
    """The agent recovers a note the ordinary top five still omit."""
    pid = "synthetic_l4crowded"
    hits = vocab_runtime.index.search(pid, "Is the patient on an anticoagulant?", k=5)["results"]
    assert len(hits) == 5 and all(hit["patient_id"] == pid for hit in hits)
    result = _ask(vocab_runtime, pid, "Is the patient on an anticoagulant?")
    print(
        {
            "returned_chunks": [hit["chunk_id"] for hit in hits],
            "returned_bm25": [hit["scores"]["bm25"] for hit in hits],
            "agent_outcome": result["outcome"],
            "agent_claims": len(result["answer"]["claims"]),
            "apixaban_in_agent_evidence": any("apixaban" in item["text"].lower() for item in result["evidence"]),
        }
    )
    assert any("apixaban" in chunk.text.lower() for chunk in vocab_runtime.index.chunks_for(pid))
    assert not any("apixaban" in hit["text"].lower() for hit in hits)
    assert result["outcome"] == "answered"
    (claim,) = result["answer"]["claims"]
    assert "CONTINUED: apixaban 5 mg twice daily." in claim["text"]
    assert claim["citations"] == ["crowded-apixaban#s1@v1"]
    (evidence,) = [e for e in result["evidence"] if e["evidence_id"] in claim["citations"]]
    assert evidence["source_ref"] == "DocumentReference/crowded-apixaban" and evidence["source_version"] == "1"
    assert len(result["evidence"]) <= 5
    assert CAVEAT in result["answer"]["uncertainty"]
    scope = " ".join(result["answer"]["uncertainty"]).lower()
    assert "not exhaustive" in scope and "not imported" in scope
    assert "returned" in scope and "source" in scope
    assert "takes no" not in scope and "is not on" not in scope


def test_new_name_conflicting_statuses_are_quoted_without_choosing_current_use(vocab_runtime):
    result = _ask(vocab_runtime, "synthetic_l4conflict", "Is the patient currently taking apixaban?")
    texts = " ".join(claim["text"] for claim in result["answer"]["claims"])
    assert "STOPPED: apixaban" in texts and "restarted" in texts
    assert any("more than one status for apixaban" in value for value in result["answer"]["uncertainty"])


@pytest.fixture(scope="module")
def vocab_epic(tmp_path_factory):
    patcher = pytest.MonkeyPatch()
    patcher.setattr(settings, "REPO_ROOT", tmp_path_factory.mktemp("l4-mock-epic"))
    patcher.delenv("CLINICAL_TRACE_DB_PATH", raising=False)
    runtimes = []
    try:
        for name, text in (
            ("alpha", "CONTINUED: apixaban 5 mg twice daily."),
            ("bravo", "STARTED: torsemide 40 mg daily."),
        ):
            pid, resources, binaries = fixture_patient(name)
            binary = f"Binary/eL4{name}"
            resources.append(_note(pid, f"eL4{name}", "18842-5", "Discharge Summary", DATE, binary))
            binaries[binary] = BinaryContent(
                reference=binary, content_type="text/html", data=f"<html><body><p>{text}</p></body></html>".encode()
            )
            snapshot = workspace.write_snapshot(
                pid, resources, binaries, retrieved_at=RETRIEVED_AT, mode="MOCK", snapshot_id=f"l4-vocabulary-{name}"
            )
            runtime = workspace.load_runtime(snapshot.path)
            runtimes.append((snapshot, runtime, snapshot.patient_ids[0]))
        yield runtimes
    finally:
        for _, runtime, _ in runtimes:
            runtime.close()
        patcher.undo()


@pytest.mark.parametrize(
    "index, question, drug",
    (
        (0, "Is the patient on apixaban?", "apixaban"),
        (0, "Is the patient on an anticoagulant?", "apixaban"),
        (1, "When was torsemide started?", "torsemide"),
    ),
)
def test_source_isolated_mock_epic_new_vocabulary_has_caveats_and_resolving_citations(
    vocab_epic, index, question, drug
):
    snapshot, runtime, pid = vocab_epic[index]
    result = runtime.agent.run(workspace.local_access(snapshot), pid, question)
    assert result["error"] is None and result["inference"]["mode"] == "deterministic"
    assert result["outcome"] == "answered" and CAVEAT in result["answer"]["uncertainty"]
    assert any(drug in claim["text"].lower() for claim in result["answer"]["claims"])
    if "anticoagulant" in question:
        scope = " ".join(result["answer"]["uncertainty"])
        assert "not exhaustive" in scope and "not imported" in scope
    assert all(item["patient_id"] == pid for item in result["evidence"])
    provenance = workspace.citation_provenance(runtime, result)
    assert provenance.get("unresolved", 0) == 0
    assert provenance.get("resolved_without_epic_provenance", 0) == 0
    assert result["answer"]["rejected_claims"] == []


def test_another_mock_source_cannot_supply_the_missing_named_drug(vocab_epic):
    snapshot, runtime, pid = vocab_epic[1]
    result = runtime.agent.run(workspace.local_access(snapshot), pid, "Is the patient on apixaban?")
    assert result["outcome"] == "abstained" and result["answer"]["claims"] == []
    scope = " ".join([result["answer"]["abstain_reason"] or "", *result["answer"]["uncertainty"]])
    assert "not imported" in scope
    assert all(item["patient_id"] == pid for item in result["evidence"])


@pytest.mark.parametrize("coverage", ("not_imported", "returned_none", "undeclared", "unrecognized"))
@pytest.mark.parametrize("term", ("anticoagulant", "blood-thinner"))
def test_administrative_class_text_is_never_medication_use_evidence(coverage, term):
    draft = _draft(f"Is the patient on a {term}?", "Anticoagulation clinic appointment was discussed.", coverage)
    assert draft.claims == [] and draft.abstain_reason
    scope = " ".join([draft.abstain_reason, *draft.uncertainty]).lower()
    assert "not exhaustive" in scope
    expected = COVERAGE.get(coverage, COVERAGE[None])
    assert expected.lower() in scope


@pytest.mark.parametrize(
    "text, drug, event",
    (
        ("Apixaban was possibly stopped.", "apixaban", "stopped"),
        ("Plan: apixaban to be stopped.", "apixaban", "stopped"),
        ("It is unclear whether torsemide was started.", "torsemide", "started"),
        ("Warfarin was not stopped and apixaban was continued.", "apixaban", "stopped"),
        ("Warfarin was stopped with apixaban continued.", "apixaban", "stopped"),
        ("Apixaban may have been stopped.", "apixaban", "stopped"),
        ("Torsemide was probably started.", "torsemide", "started"),
    ),
)
def test_uncertain_or_ambiguous_multidrug_events_abstain(text, drug, event):
    draft = _draft(f"Was {drug} {event}?", text)
    assert draft.claims == [] and draft.abstain_reason


def _order_draft(drug, status="active"):
    source = {"reference": "MedicationRequest/class-order", "version": "1", "last_updated": DATE}
    medication = {
        "medication": drug,
        "dosage": "5 mg daily",
        "status": status,
        "history": [],
        "notes": [],
        "source": source,
    }
    result = ToolResult(tool="get_medication_history", ok=True, data=[medication])
    return composer.compose_deterministic(
        "Is the patient on an anticoagulant?", ["medications"], {"get_medication_history": result}
    )


@pytest.mark.parametrize("drug", ("apixaban", "warfarin"))
def test_actual_structured_candidates_remain_order_claims_not_current_use(drug):
    draft = _order_draft(drug)
    assert [claim["kind"] for claim in draft.claims] == ["medications"]
    assert draft.claims[0]["text"].startswith("Active medication orders:")
    assert draft.claims[0]["citations"] == ["class-order@v1"]
    assert CAVEAT not in draft.uncertainty
    assert any("not exhaustive" in value for value in draft.uncertainty)


@pytest.mark.parametrize("drug", ("metformin", "torsemide", "apixabanase"))
def test_unrelated_structured_orders_never_answer_the_class_question(drug):
    draft = _order_draft(drug)
    assert draft.claims == [] and draft.abstain_reason
    scope = " ".join([draft.abstain_reason, *draft.uncertainty])
    assert "not exhaustive" in scope
    assert NONE not in scope  # other orders exist; do not falsely report no orders


def test_stopped_structured_candidate_is_not_current_medication_use():
    draft = _order_draft("apixaban", status="stopped")
    assert draft.claims[0]["text"] == "apixaban is recorded as stopped."
    assert not any("Active medication orders" in claim["text"] for claim in draft.claims)


def test_real_structured_class_answer_retains_medication_request_provenance(runtime):
    result = _ask(runtime, "synthetic_p004", "What anticoagulation is documented?")
    claims = [claim for claim in result["answer"]["claims"] if claim["kind"] in ("medications", "medication_history")]
    assert claims
    evidence = {item["evidence_id"]: item for item in result["evidence"]}
    assert all(
        evidence[citation]["source_ref"].startswith("MedicationRequest/")
        for claim in claims
        for citation in claim["citations"]
    )
    assert all(evidence[citation]["source_version"] for claim in claims for citation in claim["citations"])


def test_mixed_clause_status_does_not_invent_a_stop_for_the_continued_drug():
    draft = _draft("Is the patient currently taking apixaban?", "Warfarin was stopped, apixaban continued.")
    assert draft.claims and CAVEAT in draft.uncertainty
    assert not any("apixaban was stopped" in value for value in draft.uncertainty)
    assert not any("more than one status for apixaban" in value for value in draft.uncertainty)


@pytest.mark.parametrize(
    "passage",
    (
        "Rivaroxaban was stopped with apixaban continued.",
        "Apixaban was continued with rivaroxaban stopped.",
    ),
)
def test_unrecognized_drug_does_not_transfer_a_stop_to_a_known_drug(passage):
    draft = _draft("Why was apixaban stopped?", passage)
    assert draft.claims == [] and draft.abstain_reason


@pytest.mark.parametrize(
    "passage",
    (
        "Rivaroxaban 20 mg was stopped with apixaban continued.",
        "Apixaban was continued with rivaroxaban 20 mg stopped.",
        "Apixaban continued rivaroxaban 20 mg.",
    ),
)
def test_unknown_drug_amount_is_not_a_known_drug_dose(passage):
    draft = _draft("What dose of apixaban is documented?", passage)
    assert draft.claims == [] and draft.abstain_reason


def test_multiple_drug_event_question_does_not_answer_only_the_other_drugs_event():
    draft = _draft("Was apixaban stopped and torsemide started?", "Apixaban was continued; torsemide was started.")
    assert draft.abstain_reason or any("do not establish" in value for value in draft.uncertainty)


def test_sparse_mock_epic_class_answer_uses_only_returned_note_provenance(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "REPO_ROOT", tmp_path / "sparse-mock")
    monkeypatch.delenv("CLINICAL_TRACE_DB_PATH", raising=False)
    pid, resources, _ = fixture_patient("alpha")
    resources = [item for item in resources if item["resourceType"] in ("Patient", "Encounter")]
    ref = "Binary/eSparseClass"
    resources.append(_note(pid, "eSparseClass", "18842-5", "Discharge Summary", DATE, ref))
    binary = BinaryContent(
        reference=ref,
        content_type="text/html",
        data=b"<html><body><p>CONTINUED: apixaban 5 mg twice daily.</p></body></html>",
    )
    snapshot = workspace.write_snapshot(
        pid, resources, {ref: binary}, retrieved_at=RETRIEVED_AT, mode="MOCK", snapshot_id="l4-sparse-class"
    )
    runtime = workspace.load_runtime(snapshot.path)
    try:
        result = runtime.agent.run(
            workspace.local_access(snapshot), snapshot.patient_ids[0], "Is the patient on an anticoagulant?"
        )
        assert result["outcome"] == "answered" and CAVEAT in result["answer"]["uncertainty"]
        assert any("apixaban" in claim["text"] for claim in result["answer"]["claims"])
        assert all(item["patient_id"] == snapshot.patient_ids[0] for item in result["evidence"])
        provenance = workspace.citation_provenance(runtime, result)
        assert provenance.get("unresolved", 0) == 0
        assert provenance.get("resolved_without_epic_provenance", 0) == 0
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "passage",
    (
        "Rivaroxaban stopped apixaban continued.",
        "Rivaroxaban stopped apixaban.",
        "Apixaban rivaroxaban stopped.",
    ),
)
def test_unseparated_deferred_drug_event_never_binds_to_apixaban(passage):
    draft = _draft("Why was apixaban stopped?", passage)
    assert draft.claims == [] and draft.abstain_reason
    status = _draft("Is the patient currently taking apixaban?", passage)
    assert not any("apixaban was stopped" in value for value in status.uncertainty)


PATIENT_CONTEXT_CASES = (
    ("family", "The patient's mother takes apixaban.", False),
    ("not-prescribed", "Apixaban was discussed but not prescribed.", False),
    ("allergy", "Allergy to apixaban documented.", False),
    ("continued", "CONTINUED: apixaban 5 mg twice daily.", True),
    ("hypothetical", "If apixaban were prescribed, monitoring would be discussed.", False),
    ("future-plan", "Plan: start apixaban next week.", False),
    ("future-modal", "Apixaban may be prescribed in the future.", False),
    ("future-start", "May start apixaban next week.", False),
)
CLASS_QUESTION = "Is the patient on an anticoagulant?"


@pytest.mark.parametrize("bm25", (0.0, 1.0))
@pytest.mark.parametrize("name, passage, supported", PATIENT_CONTEXT_CASES)
def test_class_patient_context_is_not_inferred_from_an_exact_name(name, passage, supported, bm25):
    draft = _draft(CLASS_QUESTION, passage, bm25=bm25)
    if supported:
        assert any(passage in claim["text"] for claim in draft.claims)
        assert CAVEAT in draft.uncertainty and draft.abstain_reason is None
    else:
        assert draft.claims == [] and draft.abstain_reason
        scope = " ".join([draft.abstain_reason, *draft.uncertainty])
        assert "not imported" in scope and "not exhaustive" in scope


@pytest.fixture(scope="module")
def context_runtime(tmp_path_factory):
    resources = []
    for name, passage, _ in PATIENT_CONTEXT_CASES:
        pid = "synthetic_context" + name.replace("-", "")
        resources.extend([patient(pid), note("context-" + name, passage, "Progress Notes", DATE, pid=pid)])
    path = tmp_path_factory.mktemp("l4-context") / "bundle.json"
    path.write_text(
        json.dumps(
            {"resourceType": "Bundle", "type": "collection", "entry": [{"resource": item} for item in resources]}
        ),
        encoding="utf-8",
    )
    runtime = build_runtime(":memory:", fixture=path, provider=None, enable_model=False)
    assert runtime.startup_ingestion["invalid"] == 0
    runtime.records.source_coverage = {"MedicationRequest": "not_imported"}
    yield runtime
    runtime.close()


@pytest.mark.parametrize("name, passage, supported", PATIENT_CONTEXT_CASES)
def test_class_patient_context_end_to_end(context_runtime, name, passage, supported):
    pid = "synthetic_context" + name.replace("-", "")
    hits = context_runtime.index.search(pid, CLASS_QUESTION, k=5)["results"]
    assert any(passage in hit["text"] for hit in hits)  # prove this is context filtering, not a retrieval miss
    result = _ask(context_runtime, pid, CLASS_QUESTION)
    if supported:
        assert result["outcome"] == "answered" and CAVEAT in result["answer"]["uncertainty"]
        assert any(passage in claim["text"] for claim in result["answer"]["claims"])
    else:
        assert result["outcome"] == "abstained" and result["answer"]["claims"] == []
        scope = " ".join([result["answer"]["abstain_reason"] or "", *result["answer"]["uncertainty"]])
        assert "not imported" in scope and "not exhaustive" in scope


@pytest.mark.parametrize("name, passage, supported", PATIENT_CONTEXT_CASES)
def test_class_patient_context_mock_epic(tmp_path, monkeypatch, name, passage, supported):
    monkeypatch.setattr(settings, "REPO_ROOT", tmp_path / "mock")
    monkeypatch.delenv("CLINICAL_TRACE_DB_PATH", raising=False)
    pid, resources, _ = fixture_patient("alpha")
    resources = [item for item in resources if item["resourceType"] in ("Patient", "Encounter")]
    ref = "Binary/eContext"
    resources.append(_note(pid, "eContext", "18842-5", "Discharge Summary", DATE, ref))
    binary = BinaryContent(
        reference=ref, content_type="text/html", data=f"<html><body><p>{passage}</p></body></html>".encode()
    )
    snapshot = workspace.write_snapshot(
        pid, resources, {ref: binary}, retrieved_at=RETRIEVED_AT, mode="MOCK", snapshot_id="l4-context"
    )
    runtime = workspace.load_runtime(snapshot.path)
    try:
        internal = snapshot.patient_ids[0]
        hits = runtime.index.search(internal, CLASS_QUESTION, k=5)["results"]
        assert any(passage in hit["text"] for hit in hits)
        result = runtime.agent.run(workspace.local_access(snapshot), internal, CLASS_QUESTION)
        assert result["error"] is None and result["answer"]["rejected_claims"] == []
        assert all(item["patient_id"] == internal for item in result["evidence"])
        if supported:
            assert result["outcome"] == "answered" and CAVEAT in result["answer"]["uncertainty"]
            provenance = workspace.citation_provenance(runtime, result)
            assert provenance.get("unresolved", 0) == provenance.get("resolved_without_epic_provenance", 0) == 0
        else:
            assert result["outcome"] == "abstained" and result["answer"]["claims"] == []
    finally:
        runtime.close()


@pytest.mark.parametrize("title", ("Family History", "Allergies"))
def test_class_patient_context_section_heading_is_preserved_as_context(title):
    item = EvidenceItem(
        evidence_id="context@v1",
        patient_id="synthetic_context",
        source_ref="DocumentReference/context",
        source_version="1",
        text="CONTINUED: apixaban 5 mg twice daily.",
        kind="retrieved",
        title=title,
        date=DATE,
        scores={"bm25": 0.0},
    )
    results = {
        "get_medication_history": ToolResult(
            tool="get_medication_history",
            ok=True,
            data=[],
            scope={"medication_orders": "none_recorded", "coverage": "not_imported"},
        ),
        "retrieve_clinical_notes": ToolResult(tool="retrieve_clinical_notes", ok=True, evidence=[item]),
    }
    draft = composer.compose_deterministic(CLASS_QUESTION, ["medications"], results)
    assert draft.claims == [] and draft.abstain_reason


def test_class_patient_context_does_not_suppress_a_separate_patient_medication_sentence():
    draft = _draft(CLASS_QUESTION, "The patient's mother takes warfarin. CONTINUED: apixaban 5 mg twice daily.")
    assert len(draft.claims) == 1 and "CONTINUED: apixaban" in draft.claims[0]["text"]
    assert "mother" not in draft.claims[0]["text"] and CAVEAT in draft.uncertainty


def test_class_patient_context_preserves_a_stop_reason_and_conflicting_restart():
    draft = _draft(CLASS_QUESTION, "Warfarin was stopped, no longer needed. Warfarin was restarted.")
    texts = " ".join(claim["text"] for claim in draft.claims)
    assert "stopped" in texts and "restarted" in texts
    assert any("more than one status for warfarin" in value for value in draft.uncertainty)
    assert CAVEAT in draft.uncertainty


def test_class_patient_context_explicit_no_prescription_is_not_use_evidence():
    draft = _draft(CLASS_QUESTION, "No prescription for apixaban.")
    assert draft.claims == [] and draft.abstain_reason
