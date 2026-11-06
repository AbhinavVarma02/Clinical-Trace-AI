"""Medication answers when a record has no medication orders (ISSUE-2). Offline; invented or synthetic records.

The answer keeps three things apart:

* structured medication orders - none recorded; the answer says so and why, as an uncited note, because no
  resource can prove an absence (a Patient resource is never cited for it);
* the data source's import coverage - "not imported", "source returned none" or not declared, worded exactly;
* medication mentions in clinical notes - quoted verbatim and labelled as note text, not verified orders.

A question about a specific drug is answered only from a note entry for that drug that documents the asked
event; otherwise it abstains with an explanation. A documented stop does not give its reason, and a historical
entry does not give current use: both unanswered parts are stated. Records with orders are unchanged.

A status question ("Is the patient currently taking furosemide?") quotes the drug's documented status entry first,
keeps the other mentions, labels a documented stop as note text rather than verified current status, and never
chooses between conflicting statuses (L1).

An entry documents an event only when it states it as done: a plan, condition, negation, proposal or instruction
("hold furosemide if ...", "was not stopped", "Stop furosemide.") is never reported as a stop, start or change (L7).
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
# The "why" note speaks only for the quoted entry (the same note may state a reason elsewhere), and a shown date is
# labelled as the note's date, which can be later than the care it describes.
REASON_UNSTATED = ("The quoted entry documents that {drug} was {event} but does not itself state why. No reason is "
                   "inferred; the full note may state one.")
REASON_UNSTATED_MENTION = ("The quoted passage mentions {drug} but does not itself state why. No reason is inferred; "
                           "the full note may state one.")
NOTE_DATED = ("The cited note (note date {date}) documents {drug}; a note date can be later than the care it "
              "describes, and the note does not establish current use. Current medication status is unverified.")


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
def test_a_documented_stop_is_reported_but_no_reason_is_inferred(rt):
    result = ask(rt, "Why was furosemide stopped?", coverage="not_imported")
    assert result["outcome"] == "answered"
    assert any("STOPPED: furosemide 40 mg daily" in q["text"] for q in quotes(result))
    assert result["answer"]["uncertainty"] == [
        NONE, NOT_IMPORTED, CAVEAT, REASON_UNSTATED.format(drug="furosemide", event="stopped"),
    ]
    assert not re.search(r"\b(because|due to|reason (is|was) (that|to))\b", " ".join(c["text"] for c in result["answer"]["claims"]))


def test_a_stated_reason_is_quoted_as_written_and_not_inferred(rt):
    result = ask(rt, "Why was lisinopril stopped?")
    assert result["outcome"] == "answered"
    assert any("STOPPED: lisinopril because of a rise in potassium." in q["text"] for q in quotes(result))
    assert "The reason is reported only as written in the cited note; no reason is inferred." in result["answer"]["uncertainty"]
    assert "does not itself state why" not in notes_of(result)


def test_a_historical_continued_entry_does_not_establish_current_use(rt):
    result = ask(rt, "Is the patient still taking metformin?", coverage="not_imported")
    assert result["outcome"] == "answered"
    assert any("CONTINUED: metformin 1000 mg twice daily" in q["text"] for q in quotes(result))
    assert NOTE_DATED.format(date="2026-08-07", drug="metformin") in result["answer"]["uncertainty"]
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


# ------------------------------------------------------------------------- status questions (L1)
# Invented records shaped like the LIVE G3b case: in the Discharge Summary a narrative furosemide sentence comes
# before the STOPPED entry, and a Consult mentioning "his current furosemide" has the same note date.
STATUS_DS = ("He received IV furosemide with brisk diuresis over the admission. "
             "MEDICATION CHANGES THIS ADMISSION - STOPPED: furosemide 40 mg daily - STARTED: torsemide 40 mg daily - "
             "CONTINUED: metformin 1000 mg twice daily")
STATUS_CONSULT = ("He describes variable urine output on his current furosemide 40 mg daily. "
                  "Variable response on oral furosemide is consistent with gut edema.")
STATUS_MENTIONS = ("Furosemide was discussed with the patient at length.", "He asked about furosemide side effects.",
                   "Furosemide timing was reviewed with pharmacy.", "Family asked about furosemide.")
STATUS_PATIENTS = {
    "synthetic_l1same": [("l1-same-ds", STATUS_DS, "Discharge Summary", "2026-11-02T05:03:06Z"),
                         ("l1-same-consult", STATUS_CONSULT, "Consults", "2026-11-02T05:03:06Z")],
    "synthetic_l1rank": [("l1-rank-ds", STATUS_DS, "Discharge Summary", "2026-08-07T12:00:00Z")]
                        + [(f"l1-rank-m{i}", text, "Progress Notes", f"2026-09-0{i + 1}T09:00:00Z")
                           for i, text in enumerate(STATUS_MENTIONS)],
    "synthetic_l1conflict": [("l1-conflict-ds", STATUS_DS, "Discharge Summary", "2026-08-07T12:00:00Z"),
                             ("l1-conflict-pn", "Progress note: volume overload recurred; furosemide restarted at "
                              "20 mg daily.", "Progress Notes", "2026-09-01T09:00:00Z")],
    "synthetic_l1prose": [("l1-prose-pn", "Progress note: furosemide was discontinued after the creatinine rise.",
                           "Progress Notes", "2026-09-01T09:00:00Z"),
                          ("l1-prose-consult", STATUS_CONSULT, "Consults", "2026-08-01T09:00:00Z")],
    "synthetic_l1hold": [("l1-hold-pn", "Plan: hold furosemide if systolic pressure is below 90.", "Progress Notes",
                          "2026-09-01T09:00:00Z")],
    "synthetic_l1plain": [("l1-plain-consult", STATUS_CONSULT, "Consults", "2026-08-01T09:00:00Z")],
}
STATUS_QUESTIONS = ("Is the patient currently taking furosemide?", "Is the patient still on furosemide?",
                    "Is the patient taking furosemide?")
OTHER_DRUG_QUESTIONS = ("Was furosemide stopped?", "Why was furosemide stopped?", "When was furosemide started?",
                        "What dose of furosemide is documented?", "What medications are documented?")
STOP = ("The cited note (note date {date}) documents that furosemide was stopped. A documented stop is note text, not "
        "a verified current medication status; current medication status is unverified.")
CONFLICT = ("The cited notes record more than one status for furosemide (a stop and a start or continuation); the "
            "record does not establish which is current. Current medication status is unverified.")
CONTEXT = ("Other cited passages mention furosemide without a documented status; none of them establishes current "
           "use.")
AS_OF = NOTE_DATED.replace("{drug}", "furosemide")


@pytest.fixture()
def status_rt(tmp_path):
    resources = [patient(pid) for pid in STATUS_PATIENTS]
    resources += [note(did, text, title, date, pid=pid)
                  for pid, notes in STATUS_PATIENTS.items() for did, text, title, date in notes]
    bundle = tmp_path / "bundle.json"
    bundle.write_text(json.dumps({"resourceType": "Bundle", "type": "collection",
                                  "entry": [{"resource": r} for r in resources]}), encoding="utf-8")
    runtime = build_runtime(":memory:", fixture=bundle, provider=None, enable_model=False)
    yield runtime
    runtime.close()


@pytest.mark.parametrize("question", STATUS_QUESTIONS)
def test_a_status_question_quotes_the_documented_stop_first_and_keeps_the_other_mentions(status_rt, question):
    # L1: three furosemide sentences tie on word overlap; the earliest used to win and the STOPPED entry was lost.
    result = ask(status_rt, question, pid="synthetic_l1same", coverage="not_imported")
    assert result["outcome"] == "answered"
    texts = [q["text"] for q in quotes(result)]
    assert "STOPPED: furosemide 40 mg daily" in texts[0]
    assert any("his current furosemide 40 mg daily" in t for t in texts)  # the other mention is kept
    assert result["answer"]["uncertainty"] == [NONE, NOT_IMPORTED, CAVEAT, STOP.format(date="2026-11-02"), CONTEXT]
    assert AS_OF.format(date="2026-11-02") not in notes_of(result)


def test_a_stop_entry_in_a_lower_ranked_note_is_still_quoted(status_rt):
    # Four plain mentions outrank the Discharge Summary; the 3-quote limit used to drop its STOPPED entry.
    result = ask(status_rt, "Is the patient currently taking furosemide?", pid="synthetic_l1rank")
    texts = [q["text"] for q in quotes(result)]
    assert len(texts) == 3 and "STOPPED: furosemide 40 mg daily" in texts[0]
    assert sum(any(m in t for m in STATUS_MENTIONS) for t in texts) == 2  # the remaining slots keep the mentions
    assert result["answer"]["uncertainty"] == [NONE, UNDECLARED, CAVEAT, STOP.format(date="2026-08-07"), CONTEXT]


def test_conflicting_statuses_are_both_quoted_and_neither_is_chosen(status_rt):
    result = ask(status_rt, "Is the patient currently taking furosemide?", pid="synthetic_l1conflict",
                 coverage="not_imported")
    texts = [q["text"] for q in quotes(result)]
    assert any("STOPPED: furosemide 40 mg daily" in t for t in texts)
    assert any("furosemide restarted at 20 mg daily" in t for t in texts)
    assert result["answer"]["uncertainty"] == [NONE, NOT_IMPORTED, CAVEAT, CONFLICT]


def test_a_stop_written_as_prose_is_also_a_documented_stop(status_rt):
    result = ask(status_rt, "Is the patient taking furosemide?", pid="synthetic_l1prose", coverage="not_imported")
    texts = [q["text"] for q in quotes(result)]
    assert "furosemide was discontinued after the creatinine rise." in texts[0]
    assert any("his current furosemide 40 mg daily" in t for t in texts)
    assert result["answer"]["uncertainty"] == [NONE, NOT_IMPORTED, CAVEAT, STOP.format(date="2026-09-01"), CONTEXT]


@pytest.mark.parametrize("pid, date", [("synthetic_l1hold", "2026-09-01"), ("synthetic_l1plain", "2026-08-01")])
def test_without_a_documented_status_the_answer_is_unchanged(status_rt, pid, date):
    # A conditional instruction ("hold ... if") is not a documented stop, and plain mentions get today's notes only.
    result = ask(status_rt, "Is the patient currently taking furosemide?", pid=pid, coverage="not_imported")
    assert result["outcome"] == "answered"
    assert result["answer"]["uncertainty"] == [NONE, NOT_IMPORTED, CAVEAT, AS_OF.format(date=date)]
    # Isolation: other patients' STOPPED and restarted entries are never quoted.
    assert not any("STOPPED" in q["text"] or "restarted" in q["text"] for q in quotes(result))


def test_the_status_rule_changes_only_status_questions_on_entries_with_a_documented_status(status_rt, runtime,
                                                                                          monkeypatch):
    access = AccessContext(PERSONAS["clinician.demo"])

    def answers():
        out = {}
        for pid in STATUS_PATIENTS:
            for question in STATUS_QUESTIONS + OTHER_DRUG_QUESTIONS:
                r = ask(status_rt, question, pid=pid, coverage="not_imported")
                out[(pid, question)] = (r["outcome"], r["answer"]["claims"], r["answer"]["uncertainty"],
                                        r["answer"]["abstain_reason"])
        for pid in ("synthetic_p001", "synthetic_p002", "synthetic_p003", "synthetic_p004"):  # synthetic demo
            for question in STATUS_QUESTIONS + ("Is the patient still on metformin?", "Is the patient taking insulin?",
                                                "Is the patient still on warfarin?"):
                r = runtime.agent.run(access, pid, question)
                out[(pid, question)] = (r["outcome"], r["answer"]["claims"], r["answer"]["uncertainty"],
                                        r["answer"]["abstain_reason"])
        return out

    current = answers()
    monkeypatch.setattr(composer, "_status_drugs", lambda question: [])
    without = answers()
    changed = {key for key in current if current[key] != without[key]}
    assert changed == {(pid, q) for pid in ("synthetic_l1same", "synthetic_l1rank", "synthetic_l1conflict",
                                            "synthetic_l1prose") for q in STATUS_QUESTIONS}


def test_status_notes_invent_nothing(status_rt):
    for pid in STATUS_PATIENTS:
        for question in STATUS_QUESTIONS:
            result = ask(status_rt, question, pid=pid, coverage="not_imported")
            assert all(c["kind"] == "quote" for c in result["answer"]["claims"])  # quotes are the only claims
            cited_text = " ".join(e["text"] for e in result["evidence"] if e["cited"])
            for claim in result["answer"]["claims"]:
                assert all(dose in cited_text for dose in DOSE.findall(claim["text"]))
            notes = notes_of(result)
            assert not DOSE.search(notes)
            assert not re.search(r"\b(is|are|was) (not )?(taking|on)\b|\bno longer\b|\bbecause\b|\bdue to\b", notes)


# ------------------------------------------------------------------------- documented events only (L7)
# One invented note per patient. Only entries that state the event as done (past tense, not negated, conditional,
# proposed or instructed) may answer a stop/start/change question or carry a "documented stop" note.
EVENT_ENTRIES = {  # patient -> (note id, entry, documented event or None)
    "synthetic_l7holdif": ("l7-hold-if", "Plan: hold furosemide if systolic pressure is below 90.", None),
    "synthetic_l7donotstop": ("l7-do-not-stop", "Do not stop furosemide before the procedure.", None),
    "synthetic_l7notstopped": ("l7-not-stopped", "Furosemide was not stopped during the admission.", None),
    "synthetic_l7considerstop": ("l7-consider-stop", "Consider stopping furosemide if creatinine rises.", None),
    "synthetic_l7ifstopped": ("l7-if-stopped", "If furosemide is stopped, recheck weight daily.", None),
    "synthetic_l7shouldbeheld": ("l7-should-be-held", "Furosemide should be held on the morning of surgery.", None),
    "synthetic_l7startif": ("l7-start-if", "Start furosemide 20 mg if weight rises by 2 kg.", None),
    "synthetic_l7increaseif": ("l7-increase-if", "Increase furosemide to 80 mg if edema worsens.", None),
    "synthetic_l7imperative": ("l7-imperative", "Discharge instructions: Stop furosemide.", None),
    "synthetic_l7held": ("l7-held", "Furosemide was held for two days.", "stopped"),
    "synthetic_l7stoppedlist": ("l7-stopped-list", "MEDICATION CHANGES - STOPPED: furosemide 40 mg daily", "stopped"),
    "synthetic_l7discontinued": ("l7-discontinued", "Furosemide was discontinued on day three.", "stopped"),
    "synthetic_l7nolonger": ("l7-no-longer", "STOPPED: furosemide 40 mg daily, no longer needed.", "stopped"),
    "synthetic_l7started": ("l7-started", "Furosemide was started at 20 mg daily.", "started"),
    "synthetic_l7increased": ("l7-increased", "Furosemide was increased to 80 mg daily.", "changed"),
}
EVENT_QUESTIONS = {"stopped": ("Was furosemide stopped?", "Why was furosemide stopped?", "Was furosemide held?"),
                   "started": ("When was furosemide started?", "Why was furosemide started?"),
                   "changed": ("How has the furosemide dose changed?",)}
ALL_EVENT_QUESTIONS = tuple(q for questions in EVENT_QUESTIONS.values() for q in questions)
UNDOCUMENTED = [pid for pid, (_, _, event) in EVENT_ENTRIES.items() if event is None]
DOCUMENTED_STOPS = [pid for pid, (_, _, event) in EVENT_ENTRIES.items() if event == "stopped"]
DOCUMENTED = [(pid, q) for pid, (_, _, event) in EVENT_ENTRIES.items() if event for q in EVENT_QUESTIONS[event]]
NO_FUROSEMIDE_EVENT = f"The available records do not establish the requested information about furosemide. {TAIL[None]}"


@pytest.fixture(scope="module")
def events_rt(tmp_path_factory):
    resources = []
    for pid, (did, text, _) in EVENT_ENTRIES.items():
        resources += [patient(pid), note(did, text, "Progress Notes", "2026-09-01T09:00:00Z", pid=pid)]
    bundle = tmp_path_factory.mktemp("medication-events") / "bundle.json"
    bundle.write_text(json.dumps({"resourceType": "Bundle", "type": "collection",
                                  "entry": [{"resource": r} for r in resources]}), encoding="utf-8")
    runtime = build_runtime(":memory:", fixture=bundle, provider=None, enable_model=False)
    yield runtime
    runtime.close()


@pytest.mark.parametrize("pid", UNDOCUMENTED)
def test_a_plan_condition_negation_or_instruction_is_not_a_documented_event(events_rt, pid):
    # L7: these entries used to answer event questions, and a "why" question added "documents that ... was stopped".
    for question in ALL_EVENT_QUESTIONS:
        result = ask(events_rt, question, pid=pid)
        assert result["outcome"] == "abstained" and result["answer"]["claims"] == [], question
        assert result["answer"]["abstain_reason"] == NO_FUROSEMIDE_EVENT, question
        assert "documents that furosemide was" not in notes_of(result), question


@pytest.mark.parametrize("pid", UNDOCUMENTED)
def test_a_status_question_never_reports_an_undocumented_stop(events_rt, pid):
    # The entry is still quoted verbatim, with the note-date note and no "documented stop" note.
    result = ask(events_rt, "Is the patient currently taking furosemide?", pid=pid)
    assert result["outcome"] == "answered"
    assert [EVENT_ENTRIES[pid][1] in q["text"] for q in quotes(result)] == [True]
    assert result["answer"]["uncertainty"] == [NONE, UNDECLARED, CAVEAT, AS_OF.format(date="2026-09-01")]


@pytest.mark.parametrize("pid, question", DOCUMENTED)
def test_a_documented_event_is_still_answered(events_rt, pid, question):
    result = ask(events_rt, question, pid=pid)
    assert result["outcome"] == "answered"
    assert [EVENT_ENTRIES[pid][1] in q["text"] for q in quotes(result)] == [True]
    reason_unstated = REASON_UNSTATED.format(drug="furosemide", event=EVENT_ENTRIES[pid][2])
    assert (reason_unstated in result["answer"]["uncertainty"]) == question.startswith("Why")


@pytest.mark.parametrize("pid", DOCUMENTED_STOPS)
def test_a_documented_stop_still_gets_the_stop_note(events_rt, pid):
    result = ask(events_rt, "Is the patient currently taking furosemide?", pid=pid)
    assert result["answer"]["uncertainty"] == [NONE, UNDECLARED, CAVEAT, STOP.format(date="2026-09-01")]


def test_the_negation_and_condition_check_drops_no_documented_event(events_rt, monkeypatch):
    cases = [(pid, q) for pid, (_, _, event) in EVENT_ENTRIES.items() if event
             for q in ALL_EVENT_QUESTIONS + STATUS_QUESTIONS]

    def answers():
        out = {}
        for pid, question in cases:
            r = ask(events_rt, question, pid=pid)
            out[(pid, question)] = (r["outcome"], r["answer"]["claims"], r["answer"]["uncertainty"],
                                    r["answer"]["abstain_reason"])
        return out

    current = answers()
    monkeypatch.setattr(composer, "_documented", lambda entry: True)
    assert answers() == current


# ------------------------------------------------------------------------- note wording (L3, L5)
# As in the LIVE G3b Discharge Summary: the note states why furosemide was changed in a different sentence from its
# STOPPED entry, and its note date is months after the discharge it describes.
WORDING_PID = "synthetic_wording01"
WORDING_TEXT = ("Discharge date: 2026-08-07. His loop diuretic was transitioned from furosemide to torsemide for more "
                "reliable absorption. MEDICATION CHANGES - STOPPED: furosemide 40 mg daily - STARTED: torsemide 40 mg "
                "daily - CONTINUED: metformin 1000 mg twice daily")


@pytest.fixture(scope="module")
def wording_rt(tmp_path_factory):
    resources = [patient(WORDING_PID),
                 note("wording-ds", WORDING_TEXT, "Discharge Summary", "2026-11-02T05:03:06Z", pid=WORDING_PID)]
    bundle = tmp_path_factory.mktemp("medication-wording") / "bundle.json"
    bundle.write_text(json.dumps({"resourceType": "Bundle", "type": "collection",
                                  "entry": [{"resource": r} for r in resources]}), encoding="utf-8")
    runtime = build_runtime(":memory:", fixture=bundle, provider=None, enable_model=False)
    yield runtime
    runtime.close()


@pytest.mark.parametrize("question, expected", [
    ("Why was furosemide stopped?", REASON_UNSTATED.format(drug="furosemide", event="stopped")),
    ("Why is the patient on metformin?", REASON_UNSTATED_MENTION.format(drug="metformin")),
])
def test_an_unstated_reason_is_described_for_the_quoted_entry_only(wording_rt, question, expected):
    # L3: the note does give a reason, in another sentence, so the answer must not say the note states none.
    result = ask(wording_rt, question, pid=WORDING_PID, coverage="not_imported")
    assert result["outcome"] == "answered"
    assert result["answer"]["uncertainty"] == [NONE, NOT_IMPORTED, CAVEAT, expected]
    notes = notes_of(result)
    assert "the reason is unknown" not in notes and "The cited note documents that" not in notes
    assert "absorption" not in notes  # the stated reason is never restated outside a quote


def test_a_shown_date_is_labelled_as_the_note_date(wording_rt):
    # L5: the note is dated 2026-11-02 but describes a 2026-08-07 discharge; the answer must not imply that the
    # medication list held "as of" the note date.
    result = ask(wording_rt, "Is the patient still on metformin?", pid=WORDING_PID, coverage="not_imported")
    assert result["outcome"] == "answered"
    assert any("CONTINUED: metformin 1000 mg twice daily" in q["text"] for q in quotes(result))
    assert result["answer"]["uncertainty"] == [NONE, NOT_IMPORTED, CAVEAT,
                                               NOTE_DATED.format(date="2026-11-02", drug="metformin")]
    assert "as of" not in notes_of(result)
    stopped = ask(wording_rt, "Is the patient currently taking furosemide?", pid=WORDING_PID, coverage="not_imported")
    assert STOP.format(date="2026-11-02") in stopped["answer"]["uncertainty"]
