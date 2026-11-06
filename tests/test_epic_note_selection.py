"""Epic note selection (policy ``epic-note-select/1``) and note-coverage reporting (MOCK/offline only).

ISSUE-1/ISSUE-4: the importer used to keep only the newest notes by date, so in a shared sandbox full of
recent test notes the Discharge summary, History and physical and Consult notes were never imported, and
nothing said so. These tests pin the deterministic, bounded selection, the coverage report that separates
imported, cap-excluded, unavailable-body, ineligible and historical notes, and the preservation of each
note's original clinical date and encounter. Mocked transports and fixtures only; nothing contacts Epic.
"""

from __future__ import annotations

import copy
import json
import random

import httpx
import pytest

from src.interop.epic import cli, importer, settings, workspace
from src.interop.epic.client import BinaryContent, EpicSandboxClient
from src.retrieval.chunking import chunk_patient
from tests.epic_support import RETRIEVED_AT, FakeTokens, fake_epic_transport, fixture_patient

PATIENT_REF = "Patient/eFixtureAlpha0001-NotReal"
CLINICAL_NOTE = [
    {
        "coding": [
            {
                "system": "http://hl7.org/fhir/us/core/CodeSystem/us-core-documentreference-category",
                "code": "clinical-note",
            }
        ]
    }
]
LOINC = {
    "discharge_summary": ("18842-5", "Discharge summary"),
    "history_and_physical": ("34117-2", "History and physical note"),
    "consult": ("11488-4", "Consult note"),
    "progress": ("11506-3", "Progress note"),
    "procedure": ("28570-0", "Procedure note"),
}
HP_DATE = "2019-05-28T15:32:00Z"
DS_SOURCE = "DocumentReference/eFixtureDocDischarge1"  # the fixture's Discharge summary, dated 2025-03-06
OMIT = object()


def note(doc_id, kind=None, *, text=None, date="2026-11-01T10:00:00Z", status="current", doc_status="final",
         formats=("text/html",), encounter="Encounter/eFixtureEncFollowUp2", binary=None):
    concept: dict = {}
    if kind:
        code, display = LOINC[kind]
        concept["coding"] = [{"system": "http://loinc.org", "code": code, "display": display}]
    if text:
        concept["text"] = text
    document = {
        "resourceType": "DocumentReference",
        "id": doc_id,
        "status": status,
        "type": concept,
        "category": copy.deepcopy(CLINICAL_NOTE),
        "subject": {"reference": PATIENT_REF},
        "content": [{"attachment": {"contentType": f, "url": f"Binary/{binary or doc_id + 'Bin'}"}} for f in formats],
        "context": {"encounter": [{"reference": encounter}]},
    }
    if doc_status is not None:
        document["docStatus"] = doc_status
    if date is not OMIT:
        document["date"] = date
    return document


def selected_ids(documents, cap):
    return [d["id"] for d in importer.select_notes(documents, cap).selected]


# --------------------------------------------------------------------------- selection policy (pure)
def _sandbox_like_notes():
    """Older key notes and many newer progress notes, as in the shared public sandbox."""
    return [
        note("ds", "discharge_summary", date="2026-01-01T09:00:00Z"),
        note("hp", "history_and_physical", date=HP_DATE),
        note("consult", "consult", date="2026-01-02T09:00:00Z"),
        *[note(f"prog{n:02d}", "progress", date=f"2026-11-{n:02d}T10:00:00Z") for n in range(1, 10)],
    ]


def test_key_note_types_are_reserved_before_newer_progress_notes():
    documents = _sandbox_like_notes()
    newest_only = sorted(documents, key=lambda d: d["date"], reverse=True)[:10]
    assert {"ds", "hp"} - {d["id"] for d in newest_only}, "the old date-only rule would drop key notes"

    selection = importer.select_notes(documents, 10)
    chosen = [d["id"] for d in selection.selected]
    assert chosen[:3] == ["ds", "hp", "consult"]
    assert chosen[3:] == [f"prog{n:02d}" for n in range(9, 2, -1)]  # then the 7 newest progress notes
    summary = selection.summary
    assert summary["policy"] == importer.NOTE_SELECTION_POLICY and summary["cap"] == 10
    assert summary["selected"] == 10 and summary["excluded_by_cap_by_type"] == {"progress": 2}
    assert summary["selected_by_type"] == {"discharge_summary": 1, "history_and_physical": 1, "consult": 1,
                                           "progress": 7}
    assert summary["key_types_available"] == list(importer.KEY_NOTE_TYPES)
    assert summary["key_types_excluded_by_cap"] == []


def test_selection_does_not_depend_on_input_order():
    documents = _sandbox_like_notes() + [note("tieA", "progress", date="2026-11-09T10:00:00Z")]
    expected = selected_ids(documents, 10)
    for seed in range(5):
        shuffled = documents[:]
        random.Random(seed).shuffle(shuffled)
        assert selected_ids(shuffled, 10) == expected


def test_equal_times_are_broken_by_type_priority_then_epic_id():
    same = "2026-11-02T05:03:00Z"
    documents = [
        note("anchor-ds", "discharge_summary", date="2026-11-03T00:00:00Z"),
        note("second-ds", "discharge_summary", date=same),
        note("prog-b", "progress", date=same),
        note("prog-a", "progress", date=same),
    ]
    # The newest Discharge summary is the anchor. At an equal time the other Discharge summary outranks the
    # progress notes, and between equal progress notes the lower Epic id wins.
    assert selected_ids(documents, 3) == ["anchor-ds", "second-ds", "prog-a"]
    assert selected_ids(documents, 4) == ["anchor-ds", "second-ds", "prog-a", "prog-b"]


def test_note_type_uses_loinc_first_then_text_and_records_conflicts():
    assert importer.note_type(note("a", "discharge_summary")) == ("discharge_summary", False)
    for text, expected in (
        ("DISCHARGE SUMMARY", "discharge_summary"),
        ("Discharge Summary", "discharge_summary"),
        ("H&P", "history_and_physical"),
        ("History & Physical", "history_and_physical"),
        ("History and physical note", "history_and_physical"),
        ("Consults", "consult"),
        ("Consult Note", "consult"),
        ("Progress Notes", "progress"),
        ("Procedures", "procedure"),
        ("Telephone encounter", "other"),
    ):
        assert importer.note_type(note("t", text=text)) == (expected, False), text
    assert importer.note_type(note("none")) == ("other", False)
    assert importer.note_type(note("c", "progress", text="Discharge Summary")) == ("progress", True)


def test_ineligible_notes_are_reported_and_never_fetched():
    documents = [
        note("ok", "progress"),
        note("err", "discharge_summary", status="entered-in-error"),
        note("rtf", "history_and_physical", formats=("text/rtf",)),
        note("partial", "consult", date="2026-10"),
        note("undated", "consult", date=OMIT),
    ]
    selection = importer.select_notes(documents, 25)
    assert [d["id"] for d in selection.selected] == ["ok"]
    assert selection.summary["not_eligible"] == {
        "entered_in_error": 1,
        "no_fetchable_attachment": 1,
        "no_full_clinical_time": 2,
    }
    assert selection.summary["key_types_available"] == []


def test_a_final_note_is_preferred_over_a_newer_preliminary_one():
    documents = [
        note("ds-prelim", "discharge_summary", date="2026-11-05T10:00:00Z", doc_status="preliminary"),
        note("ds-final", "discharge_summary", date="2026-11-01T10:00:00Z"),
    ]
    selection = importer.select_notes(documents, 2)
    assert [d["id"] for d in selection.selected] == ["ds-final", "ds-prelim"]
    assert selection.summary["selected_not_final"] == 1


def test_recency_compares_instants_not_strings():
    eastern = note("eastern", "progress", date="2026-11-02T05:03:00-05:00")  # 10:03Z
    utc = note("utc", "progress", date="2026-11-02T08:00:00Z")
    assert utc["date"] > eastern["date"]  # a string sort would pick the earlier note
    assert selected_ids([utc, eastern], 1) == ["eastern"]


@pytest.mark.parametrize(
    ("cap", "chosen", "excluded"),
    [(2, ["ds", "hp"], ["consult"]), (0, [], ["discharge_summary", "history_and_physical", "consult"])],
)
def test_small_caps_report_the_key_types_they_exclude(cap, chosen, excluded):
    selection = importer.select_notes(_sandbox_like_notes(), cap)
    assert [d["id"] for d in selection.selected] == chosen
    assert selection.summary["key_types_excluded_by_cap"] == [
        {"consult": "consult"}.get(k, k) for k in excluded
    ]


def test_historical_notes_are_measured_against_the_newest_clinical_date():
    selection = importer.select_notes(_sandbox_like_notes(), 10)
    summary = selection.summary
    assert summary["historical_reference_date"] == "2026-11-09"  # newest note date, never retrieval time
    assert summary["historical_after_days"] == importer.HISTORICAL_AFTER_DAYS
    assert summary["selected_by_period"] == {"historical": 1, "recent": 9}


# --------------------------------------------------------------------------- bounded import and coverage
def _html(text: str) -> bytes:
    return f"<html><body><p>{text}</p></body></html>".encode()


def _scenario():
    """The fixture patient plus a 2019 encounter and H&P, a Consult and 12 newer progress notes."""
    pid, resources, binaries = fixture_patient()
    follow_up = next(r for r in resources if r["id"] == "eFixtureEncFollowUp2")
    historic = copy.deepcopy(follow_up)
    historic.update(id="eSelEncHistoric2019", period={"start": "2019-05-28T15:00:00Z", "end": "2019-05-28T16:00:00Z"})
    extra = [
        note("eSelHP2019", "history_and_physical", text="H&P", date=HP_DATE,
             encounter="Encounter/eSelEncHistoric2019"),
        note("eSelConsult", "consult", text="Consults", date="2025-03-04T09:00:00Z",
             encounter="Encounter/eFixtureEncInpatient1"),
        *[note(f"eSelProg{n:02d}", "progress", text="Progress Notes", date=f"2026-10-{n:02d}T10:00:00Z")
          for n in range(1, 13)],
        note("eSelRtfOnly", "progress", formats=("text/rtf",), date="2026-10-20T10:00:00Z"),
        note("eSelEnteredInError", "progress", status="entered-in-error", date="2026-10-21T10:00:00Z"),
        note("eSelPartialDate", "progress", date="2026-10"),
    ]
    texts = {
        "eSelHP2019": "History and physical: exertional chest pain since spring. Plan: outpatient stress test.",
        "eSelConsult": "Consult: infectious disease reviewed the antibiotic course.",
        **{f"eSelProg{n:02d}": f"Progress note {n}: routine review, stable." for n in range(1, 13)},
    }
    for doc_id, text in texts.items():
        ref = f"Binary/{doc_id}Bin"
        binaries[ref] = BinaryContent(reference=ref, content_type="text/html", data=_html(text))
    return pid, [*resources, historic, *extra], binaries


def _with_failures(base: httpx.MockTransport, statuses: dict[str, int]) -> httpx.MockTransport:
    def respond(request: httpx.Request) -> httpx.Response:
        relative = request.url.path.split("/api/FHIR/R4/", 1)[1]
        return httpx.Response(statuses[relative]) if relative in statuses else base.handler(request)

    return httpx.MockTransport(respond)


def _fetch(pid, resources, binaries, *, failures=None, seen=None, max_notes=10):
    transport = fake_epic_transport(resources, binaries, requests=seen)
    if failures:
        transport = _with_failures(transport, failures)
    client = EpicSandboxClient(FakeTokens(), transport=transport, sleep=lambda _s: None)  # type: ignore[arg-type]
    with client:
        return importer.fetch_patient(client, pid, max_notes=max_notes)


@pytest.fixture(scope="module")
def selected(tmp_path_factory):
    patcher = pytest.MonkeyPatch()
    patcher.setattr(settings, "REPO_ROOT", tmp_path_factory.mktemp("note-selection-repo"))
    patcher.delenv("CLINICAL_TRACE_DB_PATH", raising=False)
    pid, resources, binaries = _scenario()
    seen: list[httpx.Request] = []
    fetched = _fetch(pid, resources, binaries, seen=seen)
    snapshot = workspace.write_snapshot(pid, fetched.raw_resources, fetched.binaries, retrieved_at=RETRIEVED_AT,
                                       mode="MOCK", fetch_summary=fetched.summary, snapshot_id="note-select-01")
    runtime = workspace.load_runtime(snapshot.path)
    yield {"fetched": fetched, "seen": seen, "snapshot": snapshot, "runtime": runtime}
    runtime.close()
    patcher.undo()


def _mapped_id(runtime, source_reference: str) -> str:
    for resource in runtime.store.all_for_patient(runtime.store.patient_ids()[0], "DocumentReference"):
        if workspace._source_reference(resource) == source_reference:
            return resource["id"]
    raise AssertionError(f"{source_reference} was not imported")


def test_import_fetches_only_the_selected_bodies_within_the_cap(selected):
    fetched, seen = selected["fetched"], selected["seen"]
    binary_reads = [str(r.url).split("/api/FHIR/R4/", 1)[1] for r in seen if "/Binary/" in r.url.path]
    expected = {"Binary/eFixtureBinNote1", "Binary/eSelHP2019Bin", "Binary/eSelConsultBin",
                *(f"Binary/eSelProg{n:02d}Bin" for n in range(12, 5, -1))}
    assert len(binary_reads) == 10 and set(binary_reads) == expected
    assert len(seen) == 1 + 7 + 10  # Patient.Read, 7 searches (medications skipped), at most 10 notes
    assert fetched.summary["notes"] == {
        "fetched": 10,
        "no_fetchable_attachment": 1,
        "not_eligible.entered_in_error": 1,
        "not_eligible.no_full_clinical_time": 1,
        "skipped_note_cap": 5,
    }
    selection = fetched.summary["note_selection"]
    assert selection["selected_by_type"] == {"discharge_summary": 1, "history_and_physical": 1, "consult": 1,
                                             "progress": 7}
    # Newest clinical note date is 2026-10-20, so the 2025 Discharge summary and Consult and the 2019 H&P
    # are all more than 365 days older and are reported as historical.
    assert selection["historical_reference_date"] == "2026-10-20"
    assert selection["selected_by_period"] == {"historical": 3, "recent": 7}
    assert selection["key_types_not_fetched"] == []


def test_selected_notes_keep_their_original_dates_and_encounters(selected):
    runtime, snapshot = selected["runtime"], selected["snapshot"]
    raw = {r["id"]: r for r in json.loads((snapshot.path / "raw" / "resources.json").read_text(encoding="utf-8"))}
    pid = snapshot.patient_ids[0]
    for document in runtime.store.all_for_patient(pid, "DocumentReference"):
        source = raw[workspace._source_reference(document).split("/", 1)[1]]
        assert importer.note_clinical_time(document) == importer.note_clinical_time(source)
        encounter = runtime.store.get("Encounter", document["context"]["encounter"][0]["reference"].split("/")[1])
        assert workspace._source_reference(encounter) == source["context"]["encounter"][0]["reference"]
    hp_id = _mapped_id(runtime, "DocumentReference/eSelHP2019")
    hp = next(d for d in runtime.records.documents(pid) if d["id"] == hp_id)
    assert hp["date"] == HP_DATE  # never the retrieval time or another note's date
    hp_encounter = runtime.store.get("Encounter", hp["encounter"].split("/")[1])
    assert hp_encounter["period"]["start"].startswith("2019-05-28")
    hp_chunks = [c for c in chunk_patient(runtime.records, pid) if c.source_ref.startswith(f"DocumentReference/{hp_id}")]
    assert hp_chunks and all(c.date.startswith("2019-05-28") for c in hp_chunks)
    assert workspace.verify_snapshot(snapshot.path)["ok"]


def test_coverage_marks_the_2019_h_and_p_as_historical_and_preserved(selected):
    coverage = workspace.note_coverage(selected["snapshot"])
    assert coverage["policy"] == importer.NOTE_SELECTION_POLICY and coverage["cap"] == 10
    assert coverage["by_status"] == {"body_unavailable": 1, "excluded_by_cap": 5, "imported": 10, "not_eligible": 2}
    assert coverage["reasons"] == {
        "body_unavailable": {"no_html_or_plain_text_body": 1},
        "not_eligible": {"entered_in_error": 1, "no_full_clinical_time": 1},
    }
    assert coverage["by_status_and_type"]["excluded_by_cap"] == {"progress": 5}
    assert coverage["key_types_not_imported"] == []
    assert coverage["original_dates_preserved"] is True and coverage["encounter_links_preserved"] is True
    historical = [r for r in coverage["imported_notes"] if r["period"] == "historical"]
    assert {"type": "history_and_physical", "date": "2019-05-28", "period": "historical",
            "date_preserved": True, "encounter_linked": True} in historical
    assert [(r["type"], r["date"]) for r in historical] == [
        ("history_and_physical", "2019-05-28"), ("consult", "2025-03-04"), ("discharge_summary", "2025-03-06")
    ]
    assert all(r["period"] == "recent" for r in coverage["imported_notes"] if r["date"].startswith("2026"))
    assert coverage["historical_reference_date"] == "2026-10-20"  # newest clinical note date in the record
    sentence = workspace.note_coverage_sentence(coverage)
    assert "10 of 18 imported" in sentence and "5 excluded by the import cap (Progress note 5)" in sentence
    assert "1 no usable note body" in sentence and "2 not eligible" in sentence
    assert "Historical notes" in sentence and "History and physical 2019-05-28" in sentence
    assert sentence in selected["snapshot"].manifest["notes"]


def test_discharge_question_now_cites_the_imported_discharge_summary(selected):
    runtime, snapshot = selected["runtime"], selected["snapshot"]
    pid = snapshot.patient_ids[0]
    result = runtime.agent.run(workspace.local_access(snapshot), pid,
                               "What happened at discharge and at the follow-up visit?")
    ds_id = _mapped_id(runtime, DS_SOURCE)
    cited = {c for claim in result["answer"]["claims"] for c in claim["citations"]}
    assert any(c.startswith(ds_id) for c in cited)
    assert result["inference"]["mode"] == "deterministic"


def test_the_2019_h_and_p_is_cited_with_its_own_2019_date(selected):
    runtime, snapshot = selected["runtime"], selected["snapshot"]
    pid = snapshot.patient_ids[0]
    hp_id = _mapped_id(runtime, "DocumentReference/eSelHP2019")
    result = runtime.agent.run(workspace.local_access(snapshot), pid,
                               "What did the history and physical say about chest pain?")
    hp_claims = [c for c in result["answer"]["claims"] if any(x.startswith(hp_id) for x in c["citations"])]
    assert hp_claims, "the question should surface the H&P"
    for claim in hp_claims:
        assert "2019-05-28" in claim["text"] and "2026" not in claim["text"]


def test_coverage_separates_failed_fetches_unusable_bodies_and_cap_exclusions(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "REPO_ROOT", tmp_path / "repo")
    monkeypatch.delenv("CLINICAL_TRACE_DB_PATH", raising=False)
    pid, resources, binaries = _scenario()
    binaries["Binary/eSelProg12Bin"] = BinaryContent(reference="Binary/eSelProg12Bin", content_type="text/html",
                                                     data=b"<html><body></body></html>")
    fetched = _fetch(pid, resources, binaries, failures={"Binary/eFixtureBinNote1": 403})
    assert fetched.summary["notes"]["failed"] == 1 and fetched.summary["notes"]["failed.forbidden"] == 1
    assert fetched.summary["note_selection"]["key_types_not_fetched"] == ["discharge_summary"]
    snapshot = workspace.write_snapshot(pid, fetched.raw_resources, fetched.binaries, retrieved_at=RETRIEVED_AT,
                                       mode="MOCK", fetch_summary=fetched.summary, snapshot_id="note-select-02")
    coverage = workspace.note_coverage(snapshot)
    assert coverage["by_status"] == {"body_unavailable": 3, "excluded_by_cap": 5, "imported": 8, "not_eligible": 2}
    assert coverage["reasons"]["body_unavailable"] == {
        "fetch_failed": 1,
        "fetched_body_unusable": 1,
        "no_html_or_plain_text_body": 1,
    }
    assert coverage["key_types_not_imported"] == ["discharge_summary"]
    assert coverage["key_notes_not_imported"] == [
        {"type": "discharge_summary", "date": "2025-03-06", "period": "historical", "status": "body_unavailable"}
    ]
    sentence = workspace.note_coverage_sentence(coverage)
    assert "Key note types present in Epic but not imported: Discharge summary." in sentence
    assert workspace.verify_snapshot(snapshot.path)["ok"]


def test_coverage_of_snapshots_without_a_recorded_selection():
    pid, resources, _binaries = _scenario()
    documents = [r for r in resources if r["resourceType"] == "DocumentReference"]
    eligible = [d for d in documents if importer.note_ineligibility(d) is None]
    old_fetch = {"notes": {"fetched": 0, "skipped_note_cap": len(eligible)}}
    coverage = workspace.summarize_note_coverage(resources, [], set(), old_fetch)
    assert coverage["policy"] is None and coverage["by_status"]["excluded_by_cap"] == len(eligible)
    assert "selection policy not recorded" in workspace.note_coverage_sentence(coverage)
    with_failures = workspace.summarize_note_coverage(resources, [], set(), {"notes": {"failed": 2}})
    assert with_failures["by_status"]["not_fetched_unattributed"] == len(eligible)


def test_cli_show_and_review_report_coverage_without_identifiers_or_text(selected, capsys):
    snapshot_id = selected["snapshot"].manifest["snapshot_id"]
    assert cli.main(["workspace", "show", snapshot_id]) == 0
    shown = capsys.readouterr().out
    view = json.loads(shown)
    assert view["note_coverage"]["by_status"]["imported"] == 10
    assert sum(n.startswith("Clinical notes:") for n in view["notes"]) == 1
    assert cli.main(["workspace", "review", snapshot_id, "--question", "Summarize this patient's hospital course"]) == 0
    reviewed = capsys.readouterr().out
    payload = json.loads(reviewed)
    assert payload["note_coverage"] == view["note_coverage"]
    assert sum(n.startswith("Clinical notes:") for n in payload["scope_notes"]) == 1
    for forbidden in ("eFixtureAlpha0001", "Fixture Alpha", "chest pain", "pneumonia", "eSelHP2019"):
        assert forbidden not in shown and forbidden not in reviewed
