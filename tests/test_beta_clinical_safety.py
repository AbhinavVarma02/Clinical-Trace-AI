"""Owner-requested review freshness and note-coverage safety regressions."""

from __future__ import annotations

import json

import pytest

from tests.test_beta_generations import (
    ALPHA,
    BRAVO,
    ORIGIN,
    PATIENT_A,
    generations as generations,
    harness as harness,
    keys as keys,
)


@pytest.fixture
def case(request):
    return request.getfixturevalue("generations")


def inpatient_metadata(h, mode="summary", *, end=True):
    resources = h.resources[PATIENT_A]
    encounter = next(item for item in resources if item["resourceType"] == "Encounter")
    encounter["class"]["code"] = "IMP"
    if not end:
        encounter["period"].pop("end")
    document = next(item for item in resources if item["resourceType"] == "DocumentReference")
    document["type"] = (
        {"coding": [{"system": "http://loinc.org", "code": "18842-5", "display": "Discharge Summary"}]}
        if mode == "summary"
        else {"text": "Progress note"}
    )
    document["context"] = {"encounter": [{"reference": "Encounter/visit"}]}
    h.resources[PATIENT_A] = [
        item
        for item in resources
        if item["resourceType"] != "DiagnosticReport"
        and not (mode == "none" and item["resourceType"] == "DocumentReference")
    ]


def ask(h, client, gid, question):
    return client.post(
        f"{ALPHA}/generations/{gid}/runs",
        json={"question": question},
        headers={"Origin": ORIGIN, "X-CSRF-Token": h.csrf},
    )


@pytest.mark.parametrize("existing", [False, True])
def test_fresh_review_is_authorized_and_idempotent(case, existing):
    h = case
    with h.generation_client() as client:
        gid = h.refresh_generation(client)["active"]["generation_id"]
        run = h.run_generation(client, gid).json()
        path = f"{ALPHA}/generations/{gid}/runs/{run['run_id']}/review"
        first = h.post(client, path).json() if existing else None
        response = h.post(client, path)
        assert response.status_code == 200
        review = response.json()
        assert review["status"] == "pending_review"
        assert review["artifact"]["content"]["evidence_context"] == run["evidence_context"]
        if first is not None:
            assert review["artifact"]["content_hash"] == first["artifact"]["content_hash"]
            assert review["review_id"] == first["review_id"]


@pytest.mark.parametrize("state", ["cached", "stale", "rolled_back"])
@pytest.mark.parametrize("existing", [False, True])
def test_cached_review_requires_acknowledgement_on_every_submission(case, state, existing):
    h = case
    with h.generation_client() as client:
        gid = h.refresh_generation(client)["active"]["generation_id"]
        run = h.run_generation(client, gid).json()
        path = f"{ALPHA}/generations/{gid}/runs/{run['run_id']}/review"
        previous = h.post(client, path).json() if existing else None
        second = h.refresh_generation(client)["active"]["generation_id"]
        assert h.select_generation(client, second).status_code == 200
        if state == "stale":
            h.clock += 86401
            h.login(client)
        elif state == "rolled_back":
            assert h.post(client, f"{ALPHA}/generations/{gid}/rollback").status_code == 200
        assert h.select_generation(client, gid, ack=True).json()["serving"] == state
        for _ in range(2):
            response = h.post(client, path)
            assert response.status_code == 409
            assert response.json()["error"]["code"] == "cache_acknowledgement_required"
        assert h.post(client, path, **{"X-Beta-Cache-Ack": second}).status_code == 409
        accepted = h.post(client, path, **{"X-Beta-Cache-Ack": "1"})
        assert accepted.status_code == 200
        review = accepted.json()
        assert review["artifact"]["content"]["evidence_context"] == run["evidence_context"]
        repeated = h.post(client, path, **{"X-Beta-Cache-Ack": "1"}).json()
        assert repeated["artifact"]["content_hash"] == review["artifact"]["content_hash"]
        assert repeated["review_id"] == review["review_id"]
        if previous is not None:
            assert review["artifact"]["content_hash"] == previous["artifact"]["content_hash"]
            assert review["review_id"] == previous["review_id"]


@pytest.mark.parametrize("existing", [False, True])
def test_expired_generation_rejects_new_review_and_retains_history(case, existing):
    h = case
    with h.generation_client() as client:
        gid = h.refresh_generation(client)["active"]["generation_id"]
        run = h.run_generation(client, gid).json()
        path = f"{ALPHA}/generations/{gid}/runs/{run['run_id']}/review"
        previous = h.post(client, path).json() if existing else None
        h.clock += 7 * 86400 + 1
        h.login(client)
        for headers in ({}, {"X-Beta-Cache-Ack": "1"}):
            for _ in range(2):
                response = h.post(client, path, **headers)
                assert response.status_code == 410
                assert response.json()["error"]["code"] == "generation_expired"
        historical = client.get(f"{ALPHA}/generations/{gid}/runs/{run['run_id']}")
        assert historical.status_code == 200
        assert historical.json()["evidence_context"] == run["evidence_context"]
        if previous is not None:
            historical_review = client.get(f"{ALPHA}/generations/{gid}/reviews/{previous['review_id']}")
            assert historical_review.status_code == 200
            assert historical_review.json()["artifact"]["content_hash"] == previous["artifact"]["content_hash"]


def test_review_acknowledgement_keeps_auth_csrf_patient_and_reviewer_gates(case):
    h = case
    with h.generation_client() as client:
        gid = h.refresh_generation(client)["active"]["generation_id"]
        run = h.run_generation(client, gid).json()
        path = f"{ALPHA}/generations/{gid}/runs/{run['run_id']}/review"
        assert client.post(path, headers={"X-Beta-Cache-Ack": "1"}).status_code == 403
        assert h.post(client, path, **{"X-CSRF-Token": "forged", "X-Beta-Cache-Ack": "1"}).status_code == 403
        assert h.post(client, path, Origin="https://evil.invalid", **{"X-Beta-Cache-Ack": "1"}).status_code == 403
        assert h.post(client, path.replace(ALPHA, BRAVO), **{"X-Beta-Cache-Ack": "1"}).status_code == 404
        accepted = h.post(client, path).json()
        decide = f"{ALPHA}/generations/{gid}/reviews/{accepted['review_id']}/decide"
        assert h.post(client, decide).status_code == 403
        assert h.post(client, ALPHA + "/revoke").status_code == 204
        assert h.post(client, path, **{"X-Beta-Cache-Ack": "1"}).status_code == 404
        h.post(client, "/beta/auth/logout")
        assert h.post(client, path, **{"X-Beta-Cache-Ack": "1"}).status_code == 401


@pytest.mark.parametrize("mode", ["summary", "other", "none"])
@pytest.mark.parametrize("question", ["What information is missing from this record?", "Summarize this patient."])
def test_metadata_only_notes_never_become_cited_absence_claims(case, mode, question):
    h = case
    inpatient_metadata(h, mode)
    with h.generation_client() as client:
        gid = h.refresh_generation(client)["active"]["generation_id"]
        response = ask(h, client, gid, question)
        assert response.status_code == 200
        result = response.json()
        false_evidence = [item for item in result["evidence"] if item["title"] == "discharge_summary_missing"]
        assert not false_evidence, [(item["source_ref"], item["text"]) for item in false_evidence]
        answer = json.dumps(result["answer"]).lower()
        assert "no discharge summary found" not in answer
        assert "clinical note bodies were not imported" in answer
        assert "does not establish" in answer
        if mode == "summary":
            assert "discharge-summary document metadata was returned" in answer
        elif mode == "other":
            assert "no matching discharge-summary metadata was returned" in answer
        else:
            assert "clinical-note metadata search returned no document metadata" in answer
        assert all("/Binary/" not in str(call.url) for call in h.calls)


@pytest.mark.parametrize("mode", ["summary", "other", "none"])
def test_clinical_records_and_completeness_tool_use_explicit_note_coverage(case, mode):
    from src.agent.tools import ToolContext, run_tool
    from src.interop.epic.beta_catalog import Catalog
    from src.interop.epic.beta_generations import Generations

    h = case
    inpatient_metadata(h, mode)
    with h.generation_client() as client:
        gid = h.refresh_generation(client)["active"]["generation_id"]
    catalog = Catalog(h.db, lambda: h.clock)
    store = Generations(catalog, h.generation_root)
    try:
        runtime, manifest = store.runtime("mock-alpha", gid)
        pid = manifest["patient_id"]
        assert not [f for f in runtime.records.missing_information(pid) if f["category"] == "discharge_summary_missing"]
        assert runtime.records.source_coverage["DocumentReference"] == "not_imported"
        assert runtime.records.documents(pid) == []
        tool = run_tool(
            ToolContext(
                access=store.access("mock-alpha", gid, pid),
                policy=runtime.policy,
                records=runtime.records,
                index=runtime.index,
                patient_id=pid,
            ),
            "detect_missing_information",
            {},
        )
        assert tool.ok
        assert not [item for item in tool.evidence if item.title == "discharge_summary_missing"]
        assert tool.scope["note_bodies"] == "not_imported"
        assert tool.scope["document_metadata"] == ("returned_none" if mode == "none" else "returned")
        assert tool.scope["discharge_summary_metadata"] == ("returned" if mode == "summary" else "returned_none")
    finally:
        store.close()
        catalog.close()


def test_real_encounter_end_gap_remains_when_note_bodies_are_excluded(case):
    h = case
    inpatient_metadata(h, end=False)
    with h.generation_client() as client:
        gid = h.refresh_generation(client)["active"]["generation_id"]
        result = ask(h, client, gid, "What information is missing from this record?").json()
        assert any(item["title"] == "encounter_end_missing" for item in result["evidence"])
        assert not any(item["title"] == "discharge_summary_missing" for item in result["evidence"])


@pytest.mark.parametrize("with_document", [False, True])
def test_fully_imported_fixture_discharge_checks_keep_existing_behavior(with_document):
    from tests.test_discharge_summary_detection import encounter, missing_discharge, note

    resources = [encounter()]
    if with_document:
        resources.append(note({"coding": [{"system": "http://loinc.org", "code": "18842-5"}]}))
    assert bool(missing_discharge(*resources)) is not with_document
