"""ISSUE-2 on the Epic pathway (MOCK/offline): medication answers on a snapshot whose medication orders were not
imported (A1), as in the LIVE G3b snapshot.

Invented resources only; nothing contacts Epic. The snapshot manifest says medication import was off, so the
answer must say orders were not imported, quote the Discharge Summary's medication entries as note text, answer
a drug question only from an entry documenting the asked event, and never cite the Patient as proof of absence.
"""

from __future__ import annotations

import pytest

from src.interop.epic import settings, workspace
from src.interop.epic.client import BinaryContent
from tests.epic_support import RETRIEVED_AT, fixture_patient

DS_SOURCE = "DocumentReference/eMedDischarge2026"
PROC_SOURCE = "DocumentReference/eMedProcedure2026"
DS_HTML = ("<html><body><p>HOSPITAL DISCHARGE SUMMARY</p><p>He had missed two diuretic doses before admission.</p>"
           "<p>MEDICATION CHANGES THIS ADMISSION - STOPPED: "
           "furosemide 40 mg daily - STARTED: torsemide 40 mg daily - CONTINUED: metformin 1000 mg twice daily, "
           "apixaban 5 mg twice daily</p><p>FOLLOW-UP Cardiology clinic within 7 days.</p></body></html>")
PROC_HTML = ("<html><body><p>Given documented typical atrial flutter, a linear lesion set was delivered across "
             "the isthmus.</p></body></html>")
NONE = "No medication orders are recorded in this record."
NOT_IMPORTED = ("Structured medication orders were not imported from the data source for this record, so the "
                "absence of orders here does not show that none exist at the source.")
CAVEAT = ("Quoted note passages are clinical note text, not verified medication orders; any medication names, doses "
          "and dates in them are as written in the note, and their current status is not confirmed.")


def _note(pid, doc_id, loinc, display, date, binary):
    return {
        "resourceType": "DocumentReference", "id": doc_id, "status": "current", "docStatus": "final",
        "type": {"text": display, "coding": [{"system": "http://loinc.org", "code": loinc, "display": display}]},
        "category": [{"coding": [{"system": "http://hl7.org/fhir/us/core/CodeSystem/us-core-documentreference-category",
                                  "code": "clinical-note"}]}],
        "subject": {"reference": f"Patient/{pid}"}, "date": date,
        "content": [{"attachment": {"contentType": "text/html", "url": binary}}],
        "context": {"encounter": [{"reference": "Encounter/eFixtureEncInpatient1"}]},
    }


@pytest.fixture(scope="module")
def meds(tmp_path_factory):
    patcher = pytest.MonkeyPatch()
    patcher.setattr(settings, "REPO_ROOT", tmp_path_factory.mktemp("medication-answer-repo"))
    patcher.delenv("CLINICAL_TRACE_DB_PATH", raising=False)
    pid, resources, binaries = fixture_patient()
    resources += [_note(pid, "eMedDischarge2026", "18842-5", "Discharge Summary", "2026-11-02T05:03:06Z",
                        "Binary/eMedDischargeBin"),
                  _note(pid, "eMedProcedure2026", "28570-0", "Procedures", "2026-11-05T23:56:04Z",
                        "Binary/eMedProcedureBin")]
    for ref, html in (("Binary/eMedDischargeBin", DS_HTML), ("Binary/eMedProcedureBin", PROC_HTML)):
        binaries[ref] = BinaryContent(reference=ref, content_type="text/html", data=html.encode())
    snapshot = workspace.write_snapshot(pid, resources, binaries, retrieved_at=RETRIEVED_AT, mode="MOCK",
                                       snapshot_id="medication-answer-01")
    runtime = workspace.load_runtime(snapshot.path)
    yield snapshot, runtime, snapshot.patient_ids[0]
    runtime.close()
    patcher.undo()


def _run(meds, question):
    snapshot, runtime, pid = meds
    result = runtime.agent.run(workspace.local_access(snapshot), pid, question)
    assert result["error"] is None and result["inference"]["mode"] == "deterministic"
    assert result["answer"]["rejected_claims"] == []
    if result["verification"]:
        assert result["verification"]["isolation_violations"] == 0
    assert all(e["patient_id"] == pid for e in result["evidence"])
    provenance = workspace.citation_provenance(runtime, result)
    assert provenance.get("unresolved", 0) == 0 and provenance.get("resolved_without_epic_provenance", 0) == 0
    evidence = {e["evidence_id"]: e for e in result["evidence"]}
    for claim in result["answer"]["claims"]:
        if claim["kind"] != "missing_information":  # the pre-existing gap claim is unchanged
            assert not any(evidence[c]["source_ref"].startswith("Patient/") for c in claim["citations"])
    return result, evidence


def _quote_sources(meds, result, evidence):
    _snapshot, runtime, _pid = meds
    out = []
    for claim in result["answer"]["claims"]:
        if claim["kind"] == "quote":
            rtype, rid = evidence[claim["citations"][0]]["source_ref"].split("/", 1)
            out.append((workspace._source_reference(runtime.store.get(rtype, rid)), claim["text"]))
    return out


def test_medication_coverage_follows_the_manifest():
    searched = {"operations": [{"operation": "MedicationRequest.Search", "status": "ok"}]}
    failed = {"operations": [{"operation": "MedicationRequest.Search", "status": "error"}]}
    assert workspace.medication_coverage({"include_medications": False}) == {"MedicationRequest": "not_imported"}
    # "Returned none" only when the manifest shows a completed search that returned nothing.
    assert workspace.medication_coverage({"include_medications": True, "fetch": searched,
                                          "resource_counts": {"Patient": 1}}) == {"MedicationRequest": "returned_none"}
    for fetch in (failed, {}):  # a failed or unrecorded search is never reported as "returned none"
        assert workspace.medication_coverage({"include_medications": True, "fetch": fetch,
                                              "resource_counts": {"Patient": 1}}) == {"MedicationRequest": "not_imported"}
    assert workspace.medication_coverage({"include_medications": True, "fetch": searched,
                                          "resource_counts": {"MedicationRequest": 3}}) == {}


def test_the_snapshot_declares_that_medication_orders_were_not_imported(meds):
    snapshot, runtime, pid = meds
    assert snapshot.manifest["include_medications"] is False
    assert runtime.records.source_coverage == {"MedicationRequest": "not_imported"}
    assert runtime.records.medications(pid) == []


def test_the_medication_answer_quotes_the_discharge_summary_and_states_the_import_scope(meds):
    result, evidence = _run(meds, "What medications are documented?")
    assert result["outcome"] == "answered"
    sources = _quote_sources(meds, result, evidence)
    assert any(src == DS_SOURCE and "MEDICATION CHANGES THIS ADMISSION" in text for src, text in sources)
    assert not any(src == PROC_SOURCE for src, _ in sources)  # "documented" alone is not medication evidence
    assert result["answer"]["uncertainty"] == [NONE, NOT_IMPORTED, CAVEAT]


def test_a_stopped_entry_reports_the_stop_and_leaves_the_reason_unknown(meds):
    result, evidence = _run(meds, "Why was furosemide stopped?")
    assert result["outcome"] == "answered"
    assert any(src == DS_SOURCE and "STOPPED: furosemide 40 mg daily" in text
               for src, text in _quote_sources(meds, result, evidence))
    assert ("The cited note documents that furosemide was stopped but does not state why; the reason is unknown."
            in result["answer"]["uncertainty"])


def test_a_continued_entry_answers_neither_a_stop_nor_current_use(meds):
    stopped, _ = _run(meds, "Why was metformin stopped?")
    assert stopped["outcome"] == "abstained" and stopped["answer"]["claims"] == []
    assert stopped["answer"]["abstain_reason"] == (
        "The available records do not establish the requested information about metformin. Structured medication "
        "orders were not imported, so this cannot be determined from the current evidence.")
    current, evidence = _run(meds, "Is the patient still taking metformin?")
    assert current["outcome"] == "answered"
    assert any("CONTINUED: metformin 1000 mg twice daily" in text for _, text in _quote_sources(meds, current, evidence))
    assert ("The cited note (2026-11-02) documents metformin only as of that date; it does not establish current "
            "use. Current medication status is unverified.") in current["answer"]["uncertainty"]


def test_a_documented_dose_is_quoted_from_the_discharge_summary(meds):
    result, evidence = _run(meds, "What dose of metformin is documented?")
    assert result["outcome"] == "answered"
    assert any(src == DS_SOURCE and "metformin 1000 mg twice daily" in text
               for src, text in _quote_sources(meds, result, evidence))
    assert result["answer"]["uncertainty"] == [NONE, NOT_IMPORTED, CAVEAT]


def test_the_documentation_gap_keeps_its_wording_and_gains_the_import_scope(meds):
    result, _ = _run(meds, "Is any documentation missing?")
    gaps = [c["text"] for c in result["answer"]["claims"] if c["kind"] == "missing_information"]
    assert "Documentation gap: No medication orders are recorded for this patient." in gaps
    assert NOT_IMPORTED in result["answer"]["uncertainty"]
