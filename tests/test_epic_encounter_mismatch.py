"""ISSUE-5 on the Epic pathway (MOCK/offline): a Discharge Summary stating 2026 admission/discharge dates but
linked to an open 2019 encounter, as seen in the shared public sandbox.

Hand-authored, invented resources only (no recorded Epic content); a mocked transport; nothing contacts Epic.
Import -> isolated snapshot -> unchanged records, tools, agent, verifier and review gate. The admission date is
reported as a possible inconsistency, the discharge date as incomplete encounter documentation; both cite the
Discharge Summary and the encounter, which resolve to Epic-provenanced resources of this one patient.
"""

from __future__ import annotations

import copy
import json

import pytest

import src.clinical.records as records_module
from src.clinical import consistency
from src.interop.epic import importer, settings, workspace
from src.interop.epic.client import BinaryContent, EpicSandboxClient
from tests.epic_support import RETRIEVED_AT, FakeTokens, fake_epic_transport, fixture_patient

ENC_SOURCE = "Encounter/eMisEncOpen2019"
DS_SOURCE = "DocumentReference/eMisDischarge2026"
DS_DATE = "2026-11-02T05:03:06Z"
ENC_START = "2019-05-28T15:22:00Z"
FOLLOW_UP_QUESTION = "What follow-up was planned at discharge, and is anything missing?"
DS_HTML = ("<html><body><p>HOSPITAL DISCHARGE SUMMARY</p><p>Admission date: 2026-08-05</p>"
           "<p>Discharge date: 2026-08-07</p><p>FOLLOW-UP Cardiology clinic within 7 days.</p></body></html>")


def _scenario():
    pid, resources, binaries = fixture_patient()
    inpatient = next(r for r in resources if r["id"] == "eFixtureEncInpatient1")
    open_encounter = copy.deepcopy(inpatient)
    open_encounter.update(
        id="eMisEncOpen2019", status="in-progress", period={"start": ENC_START}, type=[{"text": "Inpatient"}],
        **{"class": {"system": "urn:oid:1.2.3.4.invented", "code": "3", "display": "Admission"}},
    )
    open_encounter.pop("meta", None)
    discharge = {
        "resourceType": "DocumentReference", "id": "eMisDischarge2026", "status": "current", "docStatus": "final",
        "type": {"text": "Discharge Summary",
                 "coding": [{"system": "http://loinc.org", "code": "18842-5", "display": "Discharge summary"}]},
        "category": [{"coding": [{"system": "http://hl7.org/fhir/us/core/CodeSystem/us-core-documentreference-category",
                                  "code": "clinical-note"}]}],
        "subject": {"reference": f"Patient/{pid}"},
        "date": DS_DATE,
        "content": [{"attachment": {"contentType": "text/html", "url": "Binary/eMisDischargeBin"}}],
        "context": {"encounter": [{"reference": ENC_SOURCE}]},
    }
    binaries["Binary/eMisDischargeBin"] = BinaryContent(reference="Binary/eMisDischargeBin",
                                                        content_type="text/html", data=DS_HTML.encode())
    return pid, [*resources, open_encounter, discharge], binaries


@pytest.fixture(scope="module")
def mismatch(tmp_path_factory):
    patcher = pytest.MonkeyPatch()
    patcher.setattr(settings, "REPO_ROOT", tmp_path_factory.mktemp("encounter-mismatch-repo"))
    patcher.delenv("CLINICAL_TRACE_DB_PATH", raising=False)
    pid, resources, binaries = _scenario()
    client = EpicSandboxClient(FakeTokens(), transport=fake_epic_transport(resources, binaries),  # type: ignore[arg-type]
                               sleep=lambda _s: None)
    with client:
        fetched = importer.fetch_patient(client, pid, max_notes=10)
    snapshot = workspace.write_snapshot(pid, fetched.raw_resources, fetched.binaries, retrieved_at=RETRIEVED_AT,
                                       mode="MOCK", fetch_summary=fetched.summary, snapshot_id="enc-mismatch-01")
    runtime = workspace.load_runtime(snapshot.path)
    yield snapshot, runtime, snapshot.patient_ids[0]
    runtime.close()
    patcher.undo()


def _mapped(runtime, pid, rtype, source_reference):
    for resource in runtime.store.all_for_patient(pid, rtype):
        if workspace._source_reference(resource) == source_reference:
            return resource
    raise AssertionError(f"{source_reference} was not imported")


def _sources_of(runtime, result, claim):
    evidence = {item["evidence_id"]: item for item in result["evidence"]}
    sources = []
    for citation in claim["citations"]:
        item = evidence[citation]
        rtype, rid = item["source_ref"].split("/", 1)
        resource = runtime.store.get(rtype, rid)
        assert resource is not None and item["patient_id"] == runtime.store.patient_ids()[0]
        sources.append(workspace._source_reference(resource))
    return sources


def _run(snapshot, runtime, pid, question):
    result = runtime.agent.run(workspace.local_access(snapshot), pid, question)
    assert result["error"] is None and result["inference"]["mode"] == "deterministic"
    assert result["verification"]["status"] == "verified" and result["answer"]["rejected_claims"] == []
    assert result["verification"]["isolation_violations"] == 0
    provenance = workspace.citation_provenance(runtime, result)
    assert provenance["citations"] > 0 and provenance["resolved_to_epic_source"] == provenance["citations"]
    assert all(item["patient_id"] == pid for item in result["evidence"])
    return result


def test_source_dates_and_the_encounter_link_are_imported_unchanged(mismatch):
    snapshot, runtime, pid = mismatch
    raw = {r["id"]: r for r in json.loads((snapshot.path / "raw" / "resources.json").read_text(encoding="utf-8"))}
    ds = _mapped(runtime, pid, "DocumentReference", DS_SOURCE)
    encounter = _mapped(runtime, pid, "Encounter", ENC_SOURCE)
    assert ds["date"] == raw["eMisDischarge2026"]["date"] == DS_DATE
    assert ds["context"]["encounter"][0]["reference"] == f"Encounter/{encounter['id']}"
    assert encounter["period"] == {"start": ENC_START} and encounter["status"] == "in-progress"
    assert runtime.store.patient_ids() == [pid]
    assert workspace.verify_snapshot(snapshot.path)["ok"]


def test_the_admission_date_is_a_possible_inconsistency_citing_both_resources(mismatch):
    snapshot, runtime, pid = mismatch
    result = _run(snapshot, runtime, pid, "Are there any inconsistencies in the record?")
    claims = [c for c in result["answer"]["claims"] if c["text"].startswith("Possible inconsistency")]
    assert len(claims) == 1
    claim = claims[0]
    assert "admission date 2026-08-05" in claim["text"] and f"start {ENC_START}" in claim["text"]
    assert "may conflict" in claim["text"] and "2026-08-07" not in claim["text"]
    assert _sources_of(runtime, result, claim) == [DS_SOURCE, DS_SOURCE, ENC_SOURCE]
    assert "the record contains inconsistencies" in result["review"]["reasons"] and result["review"]["required"]


def test_the_open_encounter_discharge_date_is_a_documentation_gap_citing_both_resources(mismatch):
    snapshot, runtime, pid = mismatch
    result = _run(snapshot, runtime, pid, "Is any documentation missing?")
    gaps = [c for c in result["answer"]["claims"] if "no recorded end" in c["text"]]
    assert len(gaps) == 1
    claim = gaps[0]
    assert claim["text"].startswith("Documentation gap: ") and "discharge date 2026-08-07" in claim["text"]
    assert "not a confirmed contradiction" in claim["text"] and "2026-08-05" not in claim["text"]
    assert claim["citations"][0].startswith("check:encounter_documentation_incomplete:")
    assert _sources_of(runtime, result, claim) == [ENC_SOURCE, DS_SOURCE, ENC_SOURCE]
    assert not any(c["text"].startswith("Possible inconsistency") for c in result["answer"]["claims"])
    assert "the record contains inconsistencies" not in result["review"]["reasons"]


def test_a_review_packet_keeps_the_two_findings_apart(mismatch):
    snapshot, runtime, pid = mismatch
    result = _run(snapshot, runtime, pid, "Prepare this record for review")
    texts = [c["text"] for c in result["answer"]["claims"]]
    conflicts = [t for t in texts if t.startswith("Possible inconsistency") and "2026-08-05" in t]
    gaps = [t for t in texts if t.startswith("Documentation gap") and "2026-08-07" in t]
    assert len(conflicts) == 1 and len(gaps) == 1
    assert "2026-08-07" not in conflicts[0] and "2026-08-05" not in gaps[0]
    assert result["review"]["required"] is True


def _quotes(result):
    return [(c["text"], c["citations"]) for c in result["answer"]["claims"] if c["kind"] == "quote"]


def test_the_gaps_discharge_summary_citation_does_not_hide_its_follow_up_plan(mismatch):
    snapshot, runtime, pid = mismatch
    result = _run(snapshot, runtime, pid, FOLLOW_UP_QUESTION)
    gap = next(c for c in result["answer"]["claims"] if "no recorded end" in c["text"])
    assert _sources_of(runtime, result, gap) == [ENC_SOURCE, DS_SOURCE, ENC_SOURCE]
    ds_quotes = [c for c in result["answer"]["claims"]
                 if c["kind"] == "quote" and _sources_of(runtime, result, c) == [DS_SOURCE]]
    assert any("Cardiology clinic within 7 days" in c["text"] for c in ds_quotes), _quotes(result)


@pytest.mark.parametrize("question", [
    "Summarize this patient's hospital course",
    "Summarize the discharge and follow-up plan",
    FOLLOW_UP_QUESTION,
    "Tell me about the discharge summary",
])
def test_quote_selection_does_not_depend_on_a_gaps_supporting_citations(mismatch, monkeypatch, question):
    snapshot, runtime, pid = mismatch

    def gap_citations(result):
        return [c["citations"] for c in result["answer"]["claims"] if "no recorded end" in c["text"]]

    full = _run(snapshot, runtime, pid, question)
    monkeypatch.setattr(records_module, "encounter_documentation_gaps", lambda records, patient_id: [
        {k: v for k, v in f.items() if k != "sources"}
        for f in consistency.encounter_documentation_gaps(records, patient_id)])
    check_only = _run(snapshot, runtime, pid, question)
    assert [len(c) for c in gap_citations(full)] == [3] and [len(c) for c in gap_citations(check_only)] == [1]
    assert _quotes(full) == _quotes(check_only)
