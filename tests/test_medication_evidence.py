"""Medication answers when a record has no medication orders (ISSUE-2). Offline; invented or synthetic records.

The answer keeps three things apart:

* structured medication orders - none recorded; the answer says so and why, as an uncited note, because no
  resource can prove an absence (a Patient resource is never cited for it);
* the data source's import coverage - "not imported", "source returned none" or not declared, worded exactly;
* medication mentions in clinical notes - quoted verbatim and labelled as note text, not verified orders.

A question about a specific drug is answered only from a note entry for that drug that documents the asked
event; otherwise it abstains with an explanation. A documented stop does not give its reason, and a historical
entry does not give current use: both unanswered parts are stated. Records with orders are unchanged.
"""

from __future__ import annotations

import base64
import json
import re
import uuid

import pytest

import src.agent.composer as composer
from src.runtime import build_runtime
from src.safety.policy import PERSONAS, AccessContext, Persona

PID = "synthetic_med001"
OTHER = "synthetic_med002"
EMPTY = "synthetic_med003"
META = {"versionId": "1", "lastUpdated": "2026-11-01T00:00:00Z",
        "tag": [{"system": "urn:clinical-trace:data-origin", "code": "synthetic"}]}
# The first sentence mentions a dose but not metformin, as the LIVE G3b hospital course does ("missed diuretic
# doses"); it must not win the quote for a metformin dose question.
DS_TEXT = ("He had missed two diuretic doses before admission. "
           "MEDICATION CHANGES THIS ADMISSION - STOPPED: furosemide 40 mg daily - STARTED: torsemide 40 mg daily - "
           "CONTINUED: metformin 1000 mg twice daily, apixaban 5 mg twice daily\nFOLLOW-UP Cardiology clinic in 7 days.")
PROCEDURE_TEXT = "Given documented typical atrial flutter, a linear lesion set was delivered across the isthmus."
REASON_TEXT = "Medication changes - STOPPED: lisinopril because of a rise in potassium."
OTHER_TEXT = "Discharge medications - STARTED: warfarin 5 mg daily."

NONE = "No medication orders are recorded in this record."
NOT_IMPORTED = ("Structured medication orders were not imported from the data source for this record, so the "
                "absence of orders here does not show that none exist at the source.")
RETURNED_NONE = "The data source returned no medication orders for this patient when this record was imported."
UNDECLARED = "The absence of medication orders in this record does not show that the patient takes no medication."
CAVEAT = ("Quoted note passages are clinical note text, not verified medication orders; any medication names, doses "
          "and dates in them are as written in the note, and their current status is not confirmed.")
TAIL = {
    "not_imported": "Structured medication orders were not imported, so this cannot be determined from the current evidence.",
    "returned_none": ("The data source returned no medication orders for this patient, so this cannot be determined "
                      "from structured orders."),
    None: "No medication orders are recorded in this record, so this cannot be determined from the current evidence.",
}
COVERAGE = {"not_imported": NOT_IMPORTED, "returned_none": RETURNED_NONE, None: UNDECLARED}
DOSE = re.compile(r"\d+(?:\.\d+)?\s*(?:mg|mcg|units)\b", re.I)


def patient(pid: str) -> dict:
    return {"resourceType": "Patient", "id": pid, "meta": META, "gender": "male", "birthDate": "1950-01-01",
            "name": [{"text": "Invented Test (synthetic record)"}]}


def note(did: str, text: str, title: str, date: str, pid: str = PID) -> dict:
    return {"resourceType": "DocumentReference", "id": did, "meta": META, "status": "current",
            "type": {"text": title}, "subject": {"reference": f"Patient/{pid}"}, "date": date,
            "content": [{"attachment": {"contentType": "text/plain",
                                        "data": base64.b64encode(text.encode()).decode()}}]}


@pytest.fixture()
def rt(tmp_path):
    resources = [
        patient(PID), patient(OTHER), patient(EMPTY),
        note("med-doc-ds", DS_TEXT, "Discharge Summary", "2026-08-07T12:00:00Z"),
        note("med-doc-proc", PROCEDURE_TEXT, "Procedures", "2026-11-05T10:00:00Z"),
        note("med-doc-reason", REASON_TEXT, "Progress Notes", "2026-09-01T09:00:00Z"),
        note("med-doc-other", OTHER_TEXT, "Discharge Summary", "2026-08-07T12:00:00Z", pid=OTHER),
        note("med-doc-empty", "Progress note: stable on review, plan unchanged.", "Progress Notes",
             "2026-10-01T09:00:00Z", pid=EMPTY),
    ]
    bundle = tmp_path / "bundle.json"
    bundle.write_text(json.dumps({"resourceType": "Bundle", "type": "collection",
                                  "entry": [{"resource": r} for r in resources]}), encoding="utf-8")
    runtime = build_runtime(":memory:", fixture=bundle, provider=None, enable_model=False)
    yield runtime
    runtime.close()


def ask(runtime, question: str, pid: str = PID, coverage: str | None = None) -> dict:
    runtime.records.source_coverage = {"MedicationRequest": coverage} if coverage else {}
    persona = Persona("med.clinician", "Invented test clinician", "clinician", (pid,))
    result = runtime.agent.run(AccessContext(persona=persona, request_id=str(uuid.uuid4())), pid, question)
    assert result["error"] is None and result["inference"]["mode"] == "deterministic"
    assert result["answer"]["rejected_claims"] == []
    if result["verification"]:
        assert result["verification"]["isolation_violations"] == 0
    assert all(e["patient_id"] == pid for e in result["evidence"])
    _assert_no_patient_or_coverage_citation(result)
    return result


def _assert_no_patient_or_coverage_citation(result: dict) -> None:
    """No resource proves an absence: nothing about medication orders or coverage is cited or offered as evidence."""
    evidence = {e["evidence_id"]: e for e in result["evidence"]}
    for claim in result["answer"]["claims"]:
        if claim["kind"] in ("missing_information", "profile"):
            continue  # pre-existing claims (the medication_list_missing gap and the profile) are unchanged
        assert not any(evidence[c]["source_ref"].startswith("Patient/") for c in claim["citations"]), claim
    assert not any(e["kind"] == "source_coverage" or e["evidence_id"].startswith("check:medication_orders")
                   for e in result["evidence"])
    texts = " ".join(c["text"] for c in result["answer"]["claims"])
    assert not any(sentence in texts for sentence in (NONE, NOT_IMPORTED, RETURNED_NONE, UNDECLARED))


def quotes(result: dict) -> list[dict]:
    return [c for c in result["answer"]["claims"] if c["kind"] == "quote"]


def notes_of(result: dict) -> str:
    return " ".join(result["answer"]["uncertainty"] + [result["answer"]["abstain_reason"] or ""])


# ------------------------------------------------------------------------- general medication questions
@pytest.mark.parametrize("coverage", ["not_imported", "returned_none", None])
def test_a_general_question_quotes_note_mentions_and_states_absence_and_coverage_exactly(rt, coverage):
    result = ask(rt, "What medications are documented?", coverage=coverage)
    assert result["outcome"] == "answered"
    texts = [q["text"] for q in quotes(result)]
    assert any("MEDICATION CHANGES THIS ADMISSION" in t for t in texts)
    assert not any("atrial flutter" in t for t in texts)  # matched only the generic word "documented"
    assert result["answer"]["uncertainty"] == [NONE, COVERAGE[coverage], CAVEAT]
    for other in {NOT_IMPORTED, RETURNED_NONE, UNDECLARED} - {COVERAGE[coverage]}:
        assert other not in notes_of(result)  # the three coverage states never mix


def test_a_general_question_without_note_mentions_abstains_with_an_explanation(rt):
    result = ask(rt, "What medications are documented?", pid=EMPTY, coverage="not_imported")
    assert result["outcome"] == "abstained" and result["answer"]["claims"] == []
    assert result["answer"]["abstain_reason"] == f"{NONE} {NOT_IMPORTED}"


# ------------------------------------------------------------------------- questions naming a drug
def test_a_documented_stop_is_reported_but_its_unstated_reason_is_unknown(rt):
    result = ask(rt, "Why was furosemide stopped?", coverage="not_imported")
    assert result["outcome"] == "answered"
    assert any("STOPPED: furosemide 40 mg daily" in q["text"] for q in quotes(result))
    assert result["answer"]["uncertainty"] == [
        NONE, NOT_IMPORTED, CAVEAT,
        "The cited note documents that furosemide was stopped but does not state why; the reason is unknown.",
    ]
    assert not re.search(r"\b(because|due to|reason (is|was) (that|to))\b", " ".join(c["text"] for c in result["answer"]["claims"]))


def test_a_stated_reason_is_quoted_as_written_and_not_inferred(rt):
    result = ask(rt, "Why was lisinopril stopped?")
    assert result["outcome"] == "answered"
    assert any("STOPPED: lisinopril because of a rise in potassium." in q["text"] for q in quotes(result))
    assert "The reason is reported only as written in the cited note; no reason is inferred." in result["answer"]["uncertainty"]
    assert "the reason is unknown" not in notes_of(result)


def test_a_historical_continued_entry_does_not_establish_current_use(rt):
    result = ask(rt, "Is the patient still taking metformin?", coverage="not_imported")
    assert result["outcome"] == "answered"
    assert any("CONTINUED: metformin 1000 mg twice daily" in q["text"] for q in quotes(result))
    assert ("The cited note (2026-08-07) documents metformin only as of that date; it does not establish current "
            "use. Current medication status is unverified.") in result["answer"]["uncertainty"]
    claims = " ".join(c["text"] for c in result["answer"]["claims"] if c["kind"] != "quote")
    assert not re.search(r"\b(is|currently|still) (taking|on)\b", claims)


@pytest.mark.parametrize("coverage", ["not_imported", "returned_none", None])
def test_an_undocumented_event_abstains_with_the_exact_explanation(rt, coverage):
    # The note lists metformin as CONTINUED: it does not document a stop, so nothing answers the question.
    result = ask(rt, "Why was metformin stopped?", coverage=coverage)
    assert result["outcome"] == "abstained" and result["answer"]["claims"] == []
    assert result["answer"]["abstain_reason"] == (
        f"The available records do not establish the requested information about metformin. {TAIL[coverage]}")


def test_a_dose_question_is_answered_from_an_entry_that_states_the_dose(rt):
    # L2: "dose" alone asks what dose is documented, not whether it changed.
    result = ask(rt, "What dose of metformin is documented?", coverage="not_imported")
    assert result["outcome"] == "answered"
    assert any("CONTINUED: metformin 1000 mg twice daily" in q["text"] for q in quotes(result))
    assert result["answer"]["uncertainty"] == [NONE, NOT_IMPORTED, CAVEAT]


def test_a_dose_question_abstains_when_the_entry_states_no_dose(rt):
    # The lisinopril entry documents a stop and its reason, but no amount.
    result = ask(rt, "What dose of lisinopril is documented?")
    assert result["outcome"] == "abstained" and result["answer"]["claims"] == []
    assert result["answer"]["abstain_reason"] == (
        f"The available records do not establish the requested information about lisinopril. {TAIL[None]}")


def test_a_dose_change_question_still_needs_an_entry_documenting_a_change(rt):
    # The metformin entry states a dose but no change: the dose-documentation rule must not answer this.
    result = ask(rt, "How has the metformin dose changed?", coverage="not_imported")
    assert result["outcome"] == "abstained" and result["answer"]["claims"] == []
    assert result["answer"]["abstain_reason"] == (
        f"The available records do not establish the requested information about metformin. {TAIL['not_imported']}")


def test_another_patients_medication_note_is_never_used(rt):
    result = ask(rt, "How has the warfarin dose changed?")
    assert result["outcome"] == "abstained" and "warfarin" not in " ".join(q["text"] for q in quotes(result))
    assert result["answer"]["abstain_reason"].startswith(
        "The available records do not establish the requested information about warfarin.")
    other = ask(rt, "What medications are documented?", pid=OTHER)
    assert any("STARTED: warfarin 5 mg daily." in q["text"] for q in quotes(other))
    assert not any("furosemide" in q["text"] for q in quotes(other))


def test_nothing_is_invented_and_no_answer_says_the_patient_takes_no_medication(rt):
    for question in ("What medications are documented?", "Why was furosemide stopped?", "Why was metformin stopped?",
                     "Is the patient still taking metformin?", "Why was lisinopril stopped?"):
        result = ask(rt, question, coverage="not_imported")
        cited_text = " ".join(e["text"] for e in result["evidence"] if e["cited"])
        for claim in result["answer"]["claims"]:
            assert all(dose in cited_text for dose in DOSE.findall(claim["text"]))  # doses only as written
        assert not DOSE.search(notes_of(result))  # notes and abstentions never state a dose
        assert "takes no medication" not in notes_of(result).replace(UNDECLARED, "")


# ------------------------------------------------------------------------- unchanged behaviour
QUESTIONS_WITH_ORDERS = ("What medications are documented?", "What medications is the patient taking?",
                         "How has the warfarin dose changed?", "Why was metformin stopped?",
                         "List the patient's medications")


def test_records_with_orders_are_unchanged(runtime, monkeypatch):
    access = AccessContext(PERSONAS["clinician.demo"])

    def answers():
        out = {}
        for pid in ("synthetic_p001", "synthetic_p002", "synthetic_p004"):
            for question in QUESTIONS_WITH_ORDERS:
                r = runtime.agent.run(access, pid, question)
                out[(pid, question)] = (r["outcome"], [(c["text"], c["citations"]) for c in r["answer"]["claims"]],
                                        r["answer"]["uncertainty"], r["answer"]["abstain_reason"])
        return out

    current = answers()
    monkeypatch.setattr(composer, "_no_medication_orders", lambda results: False)
    assert answers() == current
    assert not any(NONE in " ".join(u) or CAVEAT in " ".join(u) for _, _, u, _ in current.values())


def test_the_synthetic_summary_states_the_absence_once_and_adds_no_note(runtime):
    access = AccessContext(PERSONAS["clinician.demo"])
    result = runtime.agent.run(access, "synthetic_p003", "Summarize this patient journey")
    statements = [c for c in result["answer"]["claims"] if "medication orders" in c["text"].lower()]
    assert [c["text"] for c in statements] == ["Documentation gap: No medication orders are recorded for this patient."]
    assert not any(s in notes_of(result) for s in (NONE, NOT_IMPORTED, RETURNED_NONE, UNDECLARED, CAVEAT))


def test_the_gap_answer_carries_a_declared_coverage_note_only(rt):
    declared = ask(rt, "Is any documentation missing?", coverage="not_imported")
    gap = [c["text"] for c in declared["answer"]["claims"] if c["kind"] == "missing_information"]
    assert "Documentation gap: No medication orders are recorded for this patient." in gap  # wording unchanged
    assert NOT_IMPORTED in declared["answer"]["uncertainty"]
    undeclared = ask(rt, "Is any documentation missing?")
    assert not any(s in notes_of(undeclared) for s in (NOT_IMPORTED, RETURNED_NONE, UNDECLARED))
