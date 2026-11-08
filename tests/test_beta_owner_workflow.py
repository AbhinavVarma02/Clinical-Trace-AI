"""Connected owner workflow and tests-first regressions for the MOCK Beta API."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from contextlib import closing

import httpx
import pytest

from src.governance.review import artifact_content_from_run, artifact_hash
from src.interop.epic import settings
from src.interop.epic.beta_generations import Generations
from src.interop.epic.mapping import content_sha256, internal_id
from tests.test_beta_generations import (
    BODY,
    CLINICAL_TIME,
    QUESTION,
    generations as generations,
)
from tests.test_beta_owner_refresh import (
    ALPHA,
    BEARER,
    BRAVO,
    ORIGIN,
    PATIENT_A,
    PATIENT_B,
    bundle,
    harness as harness,
    keys as keys,
)

SEARCHES = (
    ("Encounter", None),
    ("Condition", "problem-list-item"),
    ("Condition", "encounter-diagnosis"),
    ("Observation", "laboratory"),
    ("Observation", "vital-signs"),
    ("DocumentReference", "clinical-note"),
    ("DiagnosticReport", None),
)
TAMPERED_SUMMARY = "invented altered review content"


@pytest.fixture
def workflow(request):
    return request.getfixturevalue("generations")


def _expect_error(response, status, code):
    assert response.status_code == status, response.text
    assert response.json() == {"error": {"code": code}}


def _published(h, client, target=ALPHA):
    key = f"owner-workflow-{len(h.calls)}"
    admitted = h.post(client, target + "/refresh", key=key)
    assert admitted.status_code == 202, admitted.text
    polled = h.wait(client, target)
    assert polled.status_code == 200 and polled.headers["Cache-Control"] == "no-store"
    status = polled.json()
    assert status["attempt"]["attempt_id"] == admitted.json()["attempt_id"]
    assert status["attempt"]["state"] == "succeeded", status
    assert status["serving"] == "fresh"
    return status


def _lab(h, patient):
    return next(resource for resource in h.resources[patient] if resource["id"] == "laboratory")


def _review_path(h, client, gid):
    response = h.run_generation(client, gid)
    assert response.status_code == 200 and response.json()["outcome"] == "answered", response.text
    return f"{ALPHA}/generations/{gid}/runs/{response.json()['run_id']}"


def _generation_bytes(h, gid):
    root = h.generation_root / "objects" / gid
    return (root / "normalized_bundle.json").read_bytes(), (root / "manifest.json").read_bytes()


def _assert_acquisition(h, patient, status, admitted_at, call_offset):
    calls = h.calls[call_offset:]
    assert status["mode"] == "MOCK" and status["source"] == "epic-sandbox-mock"
    assert status["attempt"]["requests"] == len(calls) == 9
    assert (calls[0].method, str(calls[0].url)) == ("POST", settings.TOKEN_URL)
    assert (calls[1].method, str(calls[1].url)) == ("GET", f"{settings.FHIR_BASE}/Patient/{patient}")
    for request, (kind, category) in zip(calls[2:], SEARCHES, strict=True):
        expected_query = {"patient": patient}
        if category:
            expected_query["category"] = category
        assert request.method == "GET"
        assert str(request.url).split("?")[0] == f"{settings.FHIR_BASE}/{kind}"
        assert request.url.params.multi_items() == list(expected_query.items())
    active, attempt = status["active"], status["attempt"]
    assert attempt["admitted_at"] == attempt["started_at"] == admitted_at
    assert active["retrieved_at"] == admitted_at + 1
    assert active["promoted_at"] == attempt["finished_at"] == admitted_at + 2
    assert attempt["error_code"] is None
    assert status["last_successful_generation"] == active["generation_id"]
    operations = attempt["operations"]
    expected = {"Patient", *(f"{kind}:{category}" if category else kind for kind, category in SEARCHES)}
    assert set(operations) == expected | {"Binary", "MedicationRequest"}
    for name in expected:
        counts = operations[name]
        assert counts["execution"] == "succeeded"
        assert (counts["pages"], counts["matches"], counts["unique"]) == (1, 1, 1)
        assert (counts["duplicates"], counts["rejected"]) == (0, 0)
        outcome = ("not_imported", 0, 1) if name == "DocumentReference:clinical-note" else ("imported", 1, 0)
        assert (counts["import"], counts["imported"], counts["excluded"]) == outcome
    for name in ("Binary", "MedicationRequest"):
        assert operations[name]["execution"] == "not_requested"
        assert operations[name]["import"] == "not_imported"


def _assert_evidence(h, gid, patient, run):
    lab = _lab(h, patient)
    bundle_bytes, manifest_bytes = _generation_bytes(h, gid)
    manifest = json.loads(manifest_bytes)
    context = run["evidence_context"]
    assert context["generation_id"] == gid
    assert context["patient_id"] == run["patient_id"] == internal_id("Patient", patient)
    assert context["bundle_sha256"] == hashlib.sha256(bundle_bytes).hexdigest()
    assert context["manifest_sha256"] == hashlib.sha256(manifest_bytes).hexdigest()
    assert context["retrieved_at"] == manifest["retrieved_at"]
    assert context["source"] == "epic-sandbox-mock"
    assert run["inference"]["mode"] == "deterministic" and run["inference"]["provider"] is None
    assert run["risk"] is None and run["verification"]["status"] == "verified"

    source = f"Observation/{internal_id('Observation', lab['id'])}"
    claims = run["answer"]["claims"]
    assert claims and all(claim["citations"] for claim in claims)
    cited_ids = {cite for claim in claims for cite in claim["citations"]}
    cited = {item["evidence_id"]: item for item in run["evidence"] if item["cited"]}
    assert cited_ids == set(cited)
    assert all(
        item["patient_id"] == run["patient_id"] and item["evidence_context"] == context for item in cited.values()
    )
    value = lab["valueQuantity"]["value"]
    assert any(f"{value} mg/dL" in claim["text"] for claim in claims)
    assert {item["source_ref"] for item in cited.values()} == {source}
    for item in cited.values():
        assert item["date"] == CLINICAL_TIME and item["source_version"] == "1"
        assert f"{value} mg/dL" in item["text"]
        lineage = item["provenance"]
        assert lineage["source_reference"] == f"Observation/{lab['id']}"
        assert lineage["source_version_id"] == lab["meta"]["versionId"]
        assert lineage["source_last_updated"] == lab["meta"]["lastUpdated"]
        assert lineage["source_content_sha256"] == content_sha256(lab)
        assert lineage["snapshot_id"] == gid and lineage["mapping_version"] == context["mapping_version"]
    return bundle_bytes, manifest_bytes


def test_one_owner_session_serves_two_isolated_generations_and_pending_reviews(workflow, caplog):
    h = workflow
    h.clock = 1_794_052_800
    patients = ((ALPHA, PATIENT_A, 1.2, "mock-alpha-v7"), (BRAVO, PATIENT_B, 4.8, "mock-bravo-v9"))
    for _, patient, value, version in patients:
        lab = _lab(h, patient)
        lab["valueQuantity"]["value"] = value
        lab["meta"] = {**lab["meta"], "versionId": version}
    assert _lab(h, PATIENT_A)["id"] == _lab(h, PATIENT_B)["id"]

    respond = h.fhir_override

    async def record_retrieval_time(request):
        if "/Patient/" in request.url.path:
            h.clock += 1
        return await respond(request)

    async def record_promotion_time(phase, _gid):
        if phase == "before_publish":
            h.clock += 1

    h.fhir_override, h.hook = record_retrieval_time, record_promotion_time
    originals = {}
    with h.generation_client() as client:
        h.login(client)
        for target, patient, _, _ in patients:
            call_offset, admitted_at = len(h.calls), h.clock
            status = _published(h, client, target)
            _assert_acquisition(h, patient, status, admitted_at, call_offset)
            gid = status["active"]["generation_id"]
            selected = h.select_generation(client, gid, target)
            assert selected.status_code == 200, selected.text
            selection = selected.json()
            assert selection["generation_id"] == gid and selection["serving"] == "fresh"
            assert selection["coverage"]["operations"] == status["attempt"]["operations"]
            assert selection["coverage"]["unresolved_references"] == 1
            assert selection["coverage"]["note_bodies"] == "not_imported"

            response = h.run_generation(client, gid, target)
            assert response.status_code == 200 and response.json()["outcome"] == "answered", response.text
            run = response.json()
            evidence_bytes = _assert_evidence(h, gid, patient, run)
            root = f"{target}/generations/{gid}"
            submission = h.post(client, f"{root}/runs/{run['run_id']}/review")
            assert submission.status_code == 200, submission.text
            review = submission.json()
            assert review["status"] == "pending_review" and review["approval_valid"] is False
            assert review["artifact"]["content"] == artifact_content_from_run(run)
            assert (
                review["artifact"]["content_hash"]
                == review["artifact_hash"]
                == artifact_hash(review["artifact"]["content"])
            )
            _expect_error(
                h.post(client, f"{root}/reviews/{review['review_id']}/decide"), 403, "independent_reviewer_required"
            )
            originals[target] = (status, gid, run, review, evidence_bytes)
            assert len(h.calls) == call_offset + 9

        assert originals[ALPHA][1] != originals[BRAVO][1]
        for owner, other, other_patient in ((ALPHA, BRAVO, PATIENT_B), (BRAVO, ALPHA, PATIENT_A)):
            status, gid, run, review, evidence_bytes = originals[owner]
            other_gid, other_run, other_review = originals[other][1:4]
            root = f"{owner}/generations/{gid}"
            _expect_error(h.select_generation(client, other_gid, owner), 404, "generation_unavailable")
            for wrong_root in (root, f"{owner}/generations/{other_gid}"):
                _expect_error(client.get(f"{wrong_root}/runs/{other_run['run_id']}"), 404, "artifact_unavailable")
                _expect_error(
                    client.get(f"{wrong_root}/reviews/{other_review['review_id']}"), 404, "artifact_unavailable"
                )
            question = client.post(
                root + "/runs",
                headers={"Origin": ORIGIN, "X-CSRF-Token": h.csrf},
                json={"question": f"{QUESTION} Answer for {other_patient}."},
            )
            _expect_error(question, 403, "patient_scope")
            assert client.get(f"{root}/runs/{run['run_id']}").json() == run
            assert client.get(f"{root}/reviews/{review['review_id']}").json() == review
            assert client.get(owner + "/status").json() == status
            assert _generation_bytes(h, gid) == evidence_bytes
        assert len(h.oidc_calls) == 1 and len(h.calls) == 18
        session_cookie = client.cookies.get("__Host-beta-session")
    with closing(sqlite3.connect(h.db)) as db:
        control_audit = db.execute("SELECT at, code, attempt_id FROM audit").fetchall()
    operational = json.dumps([control_audit, originals[ALPHA][0], originals[BRAVO][0]]) + caplog.text
    for sentinel in (session_cookie, h.csrf, BEARER, PATIENT_A, PATIENT_B, QUESTION, BODY, settings.FHIR_BASE):
        assert sentinel not in operational


def test_revocation_during_question_body_blocks_pin_and_execution(workflow, monkeypatch):
    h = workflow
    executed = []
    original = Generations.execute

    def record_execution(self, runtime, manifest, identifier, question):
        executed.append(identifier)
        return original(self, runtime, manifest, identifier, question)

    monkeypatch.setattr(Generations, "execute", record_execution)
    with h.generation_client() as client:
        h.login(client)
        gid = _published(h, client)["active"]["generation_id"]
        calls = len(h.calls)

        async def revoke_during_read():
            entered, resume = asyncio.Event(), asyncio.Event()

            async def question_body():
                yield b'{"question":'
                entered.set()
                await resume.wait()
                yield json.dumps(QUESTION).encode() + b"}"

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=client.app),
                base_url=ORIGIN,
                cookies=client.cookies,
                headers={"Origin": ORIGIN, "X-CSRF-Token": h.csrf},
            ) as concurrent:
                pending = asyncio.create_task(
                    concurrent.post(f"{ALPHA}/generations/{gid}/runs", content=question_body())
                )
                try:
                    await asyncio.wait_for(entered.wait(), timeout=0.75)
                    assert (await concurrent.post(ALPHA + "/revoke")).status_code == 204
                finally:
                    resume.set()
                    response = await pending
                return response

        response = client.portal.call(revoke_during_read)
        _expect_error(response, 404, "patient_unavailable")
        assert len(h.calls) == calls
    with closing(sqlite3.connect(h.db)) as db:
        pinned = db.execute("SELECT identifier FROM clinical_pin WHERE kind = 'run'").fetchall()
    assert (pinned, executed) == ([], [])


def _review_row(h, gid, *, tamper=False):
    db_path = h.generation_root / "state" / gid / "clinical.sqlite"
    with closing(sqlite3.connect(db_path)) as db, db:
        review_id, digest, stored = db.execute(
            "SELECT review.review_id, artifact.content_hash, artifact.content FROM review JOIN artifact "
            "ON review.artifact_id = artifact.artifact_id AND review.artifact_version = artifact.version"
        ).fetchone()
        content = json.loads(stored)
        if tamper:
            content["summary"] = TAMPERED_SUMMARY
            db.execute("UPDATE artifact SET content = ?", (json.dumps(content),))
        return review_id, digest, content


def test_tampered_review_fails_closed_on_get_and_repeat_post(workflow):
    h = workflow
    with h.generation_client() as client:
        h.login(client)
        gid = _published(h, client)["active"]["generation_id"]
        run_path = _review_path(h, client, gid)
        created = h.post(client, run_path + "/review")
        assert created.status_code == 200 and created.json()["status"] == "pending_review"
    review_id, _, _ = _review_row(h, gid, tamper=True)
    with h.generation_client() as client:
        h.login(client)
        _expect_error(client.get(f"{ALPHA}/generations/{gid}/reviews/{review_id}"), 409, "generation_integrity")
        repeat = h.post(client, run_path + "/review")
        assert (repeat.status_code, TAMPERED_SUMMARY in repeat.text) == (409, False)
        _expect_error(repeat, 409, "generation_integrity")


@pytest.mark.parametrize("tamper", [False, True], ids=["intact", "tampered"])
def test_retry_after_interrupted_review_pin_write(workflow, tamper):
    h = workflow
    with h.generation_client() as client:
        h.login(client)
        gid = _published(h, client)["active"]["generation_id"]
        run_path = _review_path(h, client, gid)
        original_run = client.get(run_path).json()
    with closing(sqlite3.connect(h.db)) as db, db:
        db.execute(
            "CREATE TRIGGER interrupt_review_pin BEFORE INSERT ON clinical_pin WHEN NEW.kind = 'review' "
            "BEGIN SELECT RAISE(ABORT, 'injected review pin write interruption'); END"
        )
    with h.generation_client() as client:
        h.login(client)
        with pytest.raises(sqlite3.IntegrityError, match="injected review pin write interruption"):
            h.post(client, run_path + "/review")
    with closing(sqlite3.connect(h.db)) as db, db:
        assert db.execute("SELECT COUNT(*) FROM clinical_pin WHERE kind = 'review'").fetchone() == (0,)
        db.execute("DROP TRIGGER interrupt_review_pin")
    review_id, digest, content = _review_row(h, gid)
    assert content == artifact_content_from_run(original_run) and digest == artifact_hash(content)
    if tamper:
        _review_row(h, gid, tamper=True)
    with h.generation_client() as client:
        h.login(client)
        retry = h.post(client, run_path + "/review")
        assert client.get(run_path).json() == original_run
        if tamper:
            assert (retry.status_code, TAMPERED_SUMMARY in retry.text) == (409, False)
            _expect_error(retry, 409, "generation_integrity")
        else:
            assert retry.status_code == 200, retry.text
            restored = retry.json()
            assert restored["status"] == "pending_review" and restored["review_id"] == review_id
            assert restored["artifact"]["content_hash"] == digest and restored["artifact"]["content"] == content
            assert client.get(f"{ALPHA}/generations/{gid}/reviews/{review_id}").json() == restored
    with closing(sqlite3.connect(h.db)) as db:
        assert db.execute("SELECT COUNT(*) FROM clinical_pin WHERE kind = 'review'").fetchone() == (int(not tamper),)


def test_empty_search_is_not_credited_for_dual_category_observation(workflow):
    h = workflow
    lab = _lab(h, PATIENT_A)
    vital = next(resource for resource in h.resources[PATIENT_A] if resource["id"] == "vital-signs")
    lab["category"].extend(vital["category"])

    async def empty_vitals(request):
        if request.url.path.endswith("/Observation") and request.url.params.get("category") == "vital-signs":
            return bundle()
        return None

    h.attack = empty_vitals
    with h.generation_client() as client:
        h.login(client)
        status = _published(h, client)
        gid = status["active"]["generation_id"]
        selected = h.select_generation(client, gid)
        assert selected.status_code == 200, selected.text
        coverage = selected.json()["coverage"]["operations"]
        assert coverage == status["attempt"]["operations"]
        bundle_bytes, _ = _generation_bytes(h, gid)
        observations = [
            entry["resource"]["id"]
            for entry in json.loads(bundle_bytes)["entry"]
            if entry["resource"]["resourceType"] == "Observation"
        ]
        assert observations == [internal_id("Observation", lab["id"])]
        assert coverage["Observation:laboratory"]["imported"] == 1
        empty = coverage["Observation:vital-signs"]
        assert (empty["execution"], empty["import"], empty["matches"], empty["unique"], empty["imported"]) == (
            "succeeded",
            "returned_none",
            0,
            0,
            0,
        )


@pytest.mark.parametrize("event", ["cached_generation_selected", "generation_rolled_back"])
def test_cache_selection_and_rollback_audit_correlate_to_target(workflow, caplog, event):
    h = workflow
    with h.generation_client() as client:
        first = h.refresh_generation(client)
        second = h.refresh_generation(client)
        gid = first["active"]["generation_id"]
        calls = len(h.calls)
        if event == "cached_generation_selected":
            _expect_error(h.select_generation(client, gid), 409, "cache_acknowledgement_required")
            response = h.select_generation(client, gid, ack=True)
            assert response.status_code == 200 and response.json()["serving"] == "cached"
        else:
            response = h.post(client, f"{ALPHA}/generations/{gid}/rollback")
            assert response.status_code == 200 and response.json()["active"]["generation_id"] == gid
            assert response.json()["last_successful_generation"] == second["active"]["generation_id"]
        assert len(h.calls) == calls
    with closing(sqlite3.connect(h.db)) as db:
        events = db.execute("SELECT at, code, attempt_id FROM audit ORDER BY sequence").fetchall()
        targets = db.execute("SELECT attempt_id, generation_id FROM generation").fetchall()
    operational = json.dumps(events) + caplog.text
    for sensitive in (BODY, BEARER, PATIENT_A, PATIENT_B, QUESTION, h.csrf, settings.FHIR_BASE, "invented-code"):
        assert sensitive not in operational
    target_attempt = first["attempt"]["attempt_id"]
    assert dict(targets)[target_attempt] == gid
    assert [row for row in events if row[1] == event] == [(h.clock, event, target_attempt)]
