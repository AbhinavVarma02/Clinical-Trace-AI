"""Restart, authorization and durable integrity acceptance for the MOCK owner API."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass
from urllib.parse import parse_qs

import httpx
import pytest

from api.beta_oidc import SESSION_COOKIE
from src.agent.service import AgentService
from src.interop.epic import settings
from src.interop.epic.beta_generations import CONTINUED_S
from tests.test_beta_owner_workflow import (
    ALPHA,
    BEARER,
    BODY,
    BRAVO,
    ORIGIN,
    PATIENT_A,
    PATIENT_B,
    QUESTION,
    _expect_error,
    _generation_bytes,
    _lab,
    _published,
    generations as generations,
    harness as harness,
    keys as keys,
    workflow as workflow,
)


@pytest.fixture
def acceptance(request):
    return request.getfixturevalue("workflow")


@dataclass(frozen=True)
class History:
    run_path: str
    review_path: str
    run: dict
    review: dict


def _history(h, client, target, gid):
    answer = h.run_generation(client, gid, target)
    assert answer.status_code == 200 and answer.json()["outcome"] == "answered", answer.text
    run = answer.json()
    root = f"{target}/generations/{gid}"
    run_path = f"{root}/runs/{run['run_id']}"
    submitted = h.post(client, run_path + "/review")
    assert submitted.status_code == 200 and submitted.json()["status"] == "pending_review", submitted.text
    review = submitted.json()
    return History(run_path, f"{root}/reviews/{review['review_id']}", run, review)


def _assert_history(client, history, *, serving="fresh"):
    run = client.get(history.run_path)
    review = client.get(history.review_path)
    assert run.status_code == review.status_code == 200
    assert run.json() == {**history.run, "serving": serving}
    assert review.json() == history.review


def _session_header(client):
    token = client.cookies.get(SESSION_COOKIE)
    assert token
    return {"Cookie": f"{SESSION_COOKIE}={token}"}


def _hold_publication(h):
    armed, entered = threading.Event(), threading.Event()
    resume = asyncio.Event()
    candidates = []

    async def hold(phase, gid):
        if phase == "before_publish" and armed.is_set():
            candidates.append(gid)
            entered.set()
            await resume.wait()

    h.hook = hold
    return armed, entered, resume, candidates


def _clinical_db(h, gid):
    return h.generation_root / "state" / gid / "clinical.sqlite"


def test_two_patient_restart_preserves_history_and_missing_bundle_fails_closed(acceptance):
    h = acceptance
    _lab(h, PATIENT_B)["valueQuantity"]["value"] = 4.8
    saved = {}
    with h.generation_client() as client:
        h.login(client)
        old_cookie = _session_header(client)
        for target, expected in ((ALPHA, "1.2 mg/dL"), (BRAVO, "4.8 mg/dL")):
            status = _published(h, client, target)
            gid = status["active"]["generation_id"]
            history = _history(h, client, target, gid)
            assert any(expected in claim["text"] for claim in history.run["answer"]["claims"])
            saved[target] = (status, gid, history, _generation_bytes(h, gid))
    calls = len(h.calls)
    assert calls == 18

    with h.generation_client() as client:
        for target, (_, _, history, _) in saved.items():
            for path in (target + "/status", history.run_path, history.review_path):
                _expect_error(client.get(path, headers=old_cookie), 401, "owner_required")
        assert len(h.oidc_calls) == 1 and len(h.calls) == calls
        h.login(client)
        assert _session_header(client) != old_cookie
        for target, sibling in ((ALPHA, BRAVO), (BRAVO, ALPHA)):
            status, gid, history, evidence = saved[target]
            assert client.get(target + "/status").json() == status
            assert _generation_bytes(h, gid) == evidence
            selected = h.select_generation(client, gid, target)
            assert selected.status_code == 200 and selected.json()["retrieved_at"] == status["active"]["retrieved_at"]
            _assert_history(client, history)
            for path in (history.run_path, history.review_path):
                _expect_error(client.get(path.replace(target, sibling, 1)), 404, "artifact_unavailable")
            _expect_error(h.select_generation(client, gid, sibling), 404, "generation_unavailable")
        assert len(h.calls) == calls

    alpha_gid, alpha_history = saved[ALPHA][1:3]
    (h.generation_root / "objects" / alpha_gid / "normalized_bundle.json").unlink()
    with h.generation_client() as client:
        h.login(client)
        for path in (alpha_history.run_path, alpha_history.review_path):
            _expect_error(client.get(path), 409, "generation_integrity")
        assert client.get(BRAVO + "/status").json() == saved[BRAVO][0]
        _assert_history(client, saved[BRAVO][2])
        assert len(h.calls) == calls


def test_logout_during_question_body_prevents_pin_and_execution(acceptance, monkeypatch):
    h = acceptance
    executions = []
    original = AgentService.run

    def observe(self, *args, **kwargs):
        executions.append(kwargs["run_id"])
        return original(self, *args, **kwargs)

    monkeypatch.setattr(AgentService, "run", observe)
    with h.generation_client() as client:
        h.login(client)
        gid = _published(h, client)["active"]["generation_id"]
        calls = len(h.calls)
        cookie = _session_header(client)

        async def logout_during_body():
            waiting, resume = asyncio.Event(), asyncio.Event()

            async def body():
                yield b'{"question":'
                waiting.set()
                await resume.wait()
                yield json.dumps(QUESTION).encode() + b"}"

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=client.app),
                base_url=ORIGIN,
                headers={**cookie, "Origin": ORIGIN, "X-CSRF-Token": h.csrf},
            ) as concurrent:
                pending = asyncio.create_task(concurrent.post(f"{ALPHA}/generations/{gid}/runs", content=body()))
                try:
                    await asyncio.wait_for(waiting.wait(), timeout=0.75)
                    assert (await concurrent.post("/beta/auth/logout")).status_code == 204
                finally:
                    resume.set()
                    response = await pending
                return response

        _expect_error(client.portal.call(logout_during_body), 401, "owner_required")
        _expect_error(h.run_generation(client, gid), 401, "owner_required")
        assert len(h.calls) == calls
    with closing(sqlite3.connect(h.db)) as db:
        assert db.execute("SELECT COUNT(*) FROM clinical_pin WHERE kind = 'run'").fetchone() == (0,)
    assert executions == []


@pytest.mark.parametrize("boundary", ["patient-revocation", "logout"])
def test_authorization_loss_during_admitted_thread_denies_response_and_new_work(acceptance, monkeypatch, boundary):
    h = acceptance
    entered, resume = threading.Event(), threading.Event()
    running = []
    original = AgentService._execute

    def hold(self, access, run_id, *args, **kwargs):
        # AgentService.run has already committed the running row at this boundary.
        assert self.db.one("SELECT status FROM agent_run WHERE run_id = ?", (run_id,))["status"] == "running"
        running.append(run_id)
        entered.set()
        assert resume.wait(10), "test did not release the admitted thread"
        return original(self, access, run_id, *args, **kwargs)

    monkeypatch.setattr(AgentService, "_execute", hold)
    revoke = boundary == "patient-revocation"
    denied = (404, "patient_unavailable") if revoke else (401, "owner_required")
    with h.generation_client() as client, ThreadPoolExecutor(max_workers=1) as worker:
        h.login(client)
        gid = _published(h, client)["active"]["generation_id"]
        calls, cookie = len(h.calls), _session_header(client)
        pending = worker.submit(h.run_generation, client, gid)
        try:
            assert entered.wait(5), "admitted clinical thread did not reach the barrier"
            assert h.post(client, ALPHA + "/revoke" if revoke else "/beta/auth/logout").status_code == 204
            assert not pending.done(), "revocation unexpectedly interrupted the already-admitted thread"
            run_path = f"{ALPHA}/generations/{gid}/runs/{running[0]}"
            _expect_error(client.get(run_path, headers=cookie), *denied)
            _expect_error(h.run_generation(client, gid), *denied)
        finally:
            resume.set()
        _expect_error(pending.result(10), *denied)
        assert len(running) == 1 and len(h.calls) == calls
        h.login(client)
        if revoke:
            _expect_error(client.get(run_path), 404, "patient_unavailable")
        else:
            _expect_error(client.get(run_path), 409, "generation_integrity")
            _expect_error(h.post(client, run_path + "/review"), 409, "generation_integrity")
    with closing(sqlite3.connect(h.db)) as db:
        assert db.execute("SELECT identifier, content_hash FROM clinical_pin").fetchall() == [(running[0], None)]
    with closing(sqlite3.connect(_clinical_db(h, gid))) as db:
        assert db.execute("SELECT run_id, status, outcome FROM agent_run").fetchall() == [
            (running[0], "completed", "answered")
        ]


def test_refresh_revocation_before_publish_preserves_sibling_and_durable_grant_denial(acceptance):
    h = acceptance
    armed, entered, resume, candidates = _hold_publication(h)
    with h.generation_client() as client:
        h.login(client)
        alpha = _published(h, client, ALPHA)
        bravo = _published(h, client, BRAVO)
        bravo_gid = bravo["active"]["generation_id"]
        sibling = _history(h, client, BRAVO, bravo_gid)
        h.clock += 301
        armed.set()
        response = h.post(client, ALPHA + "/refresh", key="revoked-at-publication")
        assert response.status_code == 202
        attempt = response.json()["attempt_id"]
        try:
            assert entered.wait(5), "refresh did not reach the publication fence"
            calls = len(h.calls)
            assert h.post(client, ALPHA + "/revoke").status_code == 204
            _expect_error(client.get(ALPHA + "/status"), 404, "patient_unavailable")
            _expect_error(h.post(client, ALPHA + "/refresh", key="revoked-refresh-retry"), 404, "patient_unavailable")
            _assert_history(client, sibling)
            assert client.get(BRAVO + "/status").json() == bravo
        finally:
            client.portal.call(resume.set)
        assert len(h.calls) == calls
    with closing(sqlite3.connect(h.db)) as db:
        assert db.execute("SELECT state, error_code FROM attempt WHERE attempt_id = ?", (attempt,)).fetchone() == (
            "interrupted",
            "authorization_revoked",
        )
        assert db.execute("SELECT active, epoch FROM patient_head WHERE alias = 'mock-alpha'").fetchone() == (
            alpha["active"]["generation_id"],
            alpha["publication_epoch"] + 1,
        )
        assert db.execute("SELECT 1 FROM generation WHERE generation_id = ?", (candidates[0],)).fetchone() is None
    with h.generation_client() as client:
        h.login(client)
        _expect_error(client.get(ALPHA + "/status"), 404, "patient_unavailable")
        _expect_error(h.select_generation(client, candidates[0], BRAVO, ack=True), 404, "generation_unavailable")
        _assert_history(client, sibling)
        assert len(h.calls) == calls


@pytest.mark.parametrize(
    "damage", ["artifact-hash", "missing", "wrong-shape", "pin-digest", "pin-generation", "pin-alias", "pin-kind"]
)
def test_review_integrity_rejects_corrupt_persisted_state_without_repair(acceptance, damage):
    h = acceptance
    with h.generation_client() as client:
        h.login(client)
        gid = _published(h, client)["active"]["generation_id"]
        history = _history(h, client, ALPHA, gid)
    pin_damage = {
        "pin-digest": ("content_hash", "0" * 64),
        "pin-generation": ("generation_id", "0" * 32),
        "pin-alias": ("alias", "mock-bravo"),
        "pin-kind": ("kind", "run"),
    }
    clinical = _clinical_db(h, gid)
    if damage in pin_damage:
        field, value = pin_damage[damage]
        with closing(sqlite3.connect(h.db)) as db, db:
            assert db.execute(f"UPDATE clinical_pin SET {field} = ? WHERE kind = 'review'", (value,)).rowcount == 1
    else:
        statements = {
            "artifact-hash": "UPDATE artifact SET content_hash = ?",
            "missing": "DELETE FROM artifact WHERE content_hash = ?",
            "wrong-shape": "UPDATE artifact SET content = ?",
        }
        values = {"artifact-hash": "0" * 64, "missing": history.review["artifact"]["content_hash"], "wrong-shape": "[]"}
        with closing(sqlite3.connect(clinical)) as db, db:
            assert db.execute(statements[damage], (values[damage],)).rowcount == 1

    def persisted_state():
        with closing(sqlite3.connect(clinical)) as db:
            evidence = db.execute("SELECT * FROM artifact").fetchall(), db.execute("SELECT * FROM review").fetchall()
        with closing(sqlite3.connect(h.db)) as db:
            return evidence, db.execute("SELECT * FROM clinical_pin ORDER BY identifier").fetchall()

    damaged = persisted_state()
    with h.generation_client() as client:
        h.login(client)
        if damage in ("pin-generation", "pin-alias", "pin-kind"):
            _expect_error(client.get(history.review_path), 404, "artifact_unavailable")
        else:
            _expect_error(client.get(history.review_path), 409, "generation_integrity")
        _expect_error(h.post(client, history.run_path + "/review"), 409, "generation_integrity")
        assert client.get(history.run_path).json() == history.run
    assert persisted_state() == damaged


def test_one_observation_returned_by_both_searches_is_counted_twice_and_normalized_once(acceptance):
    h = acceptance
    lab = _lab(h, PATIENT_A)
    vital = next(item for item in h.resources[PATIENT_A] if item["id"] == "vital-signs")
    lab["category"].extend(vital["category"])
    h.resources[PATIENT_A].remove(vital)
    with h.generation_client() as client:
        h.login(client)
        status = _published(h, client)
        for name in ("Observation:laboratory", "Observation:vital-signs"):
            counts = status["attempt"]["operations"][name]
            assert (counts["matches"], counts["unique"], counts["imported"], counts["duplicates"]) == (1, 1, 1, 0)
        bundle, _ = _generation_bytes(h, status["active"]["generation_id"])
        assert sum(entry["resource"]["resourceType"] == "Observation" for entry in json.loads(bundle)["entry"]) == 1


def test_two_patient_cache_rollback_expiry_and_audit_privacy(acceptance, caplog):
    h = acceptance
    armed, entered, resume, candidates = _hold_publication(h)
    caplog.set_level(logging.INFO)
    # Keep the in-process HTTP test driver's callback URL out of application-log capture.
    caplog.set_level(logging.WARNING, logger="httpx")
    with h.generation_client() as client:
        h.login(client)
        old_cookie = _session_header(client)
        secrets = [client.cookies.get(SESSION_COOKIE), h.csrf, h.auth_request["state"][0]]
        first = _published(h, client)
        alpha_gid = first["active"]["generation_id"]
        evidence = _generation_bytes(h, alpha_gid)
        assert h.select_generation(client, alpha_gid).status_code == 200
        alpha_history = _history(h, client, ALPHA, alpha_gid)
        h.clock += 301
        bravo = _published(h, client, BRAVO)
        bravo_gid = bravo["active"]["generation_id"]
        bravo_history = _history(h, client, BRAVO, bravo_gid)

        async def deny_search(request):
            if request.url.path.endswith("/Observation"):
                return httpx.Response(403, text=BODY)
            return None

        h.attack = deny_search
        h.clock += 301
        failed = h.post(client, ALPHA + "/refresh", key="owner-acceptance-failed-refresh")
        assert failed.status_code == 202
        failed_attempt = failed.json()["attempt_id"]
        status = h.wait(client).json()
        assert status["attempt"]["attempt_id"] == failed_attempt
        assert (status["attempt"]["state"], status["attempt"]["error_code"]) == ("failed", "forbidden")
        assert status["serving"] == "cached" and status["active"] == first["active"]
        _expect_error(h.select_generation(client, alpha_gid), 409, "cache_acknowledgement_required")
        selected = h.select_generation(client, alpha_gid, ack=True)
        assert selected.status_code == 200 and selected.json()["serving"] == "cached"
        _expect_error(h.run_generation(client, alpha_gid), 409, "cache_acknowledgement_required")
        _assert_history(client, bravo_history)

        h.attack = None
        h.clock += 3601
        h.login(client)
        second = _published(h, client)
        latest = second["active"]["generation_id"]
        for path in (alpha_history.run_path, alpha_history.review_path):
            _expect_error(client.get(path.replace(alpha_gid, latest, 1)), 404, "artifact_unavailable")
        h.clock += 301
        h.login(client)
        armed.set()
        competing = h.post(client, ALPHA + "/refresh", key="owner-acceptance-competing-refresh")
        assert competing.status_code == 202
        try:
            assert entered.wait(5), "competing refresh did not reach publication"
            rolled = h.post(client, f"{ALPHA}/generations/{alpha_gid}/rollback")
            assert rolled.status_code == 200, rolled.text
            rollback = rolled.json()
            assert rollback["active"] == first["active"] and rollback["serving"] == "rolled_back"
            assert rollback["last_successful_generation"] == latest
            assert rollback["publication_epoch"] == second["publication_epoch"] + 1
        finally:
            client.portal.call(resume.set)
        fenced = h.wait(client).json()
        assert fenced["attempt"]["attempt_id"] == competing.json()["attempt_id"]
        assert (fenced["attempt"]["state"], fenced["attempt"]["error_code"]) == ("failed", "publication_fenced")
        assert fenced["active"] == first["active"]
        _expect_error(h.select_generation(client, candidates[0], ack=True), 404, "generation_unavailable")
        _expect_error(h.run_generation(client, alpha_gid), 409, "cache_acknowledgement_required")
        assert h.run_generation(client, alpha_gid, ack=True).status_code == 200
        _assert_history(client, alpha_history, serving="rolled_back")
        assert client.get(BRAVO + "/status").json() == bravo
        _assert_history(client, bravo_history)

        h.clock = first["active"]["retrieved_at"] + CONTINUED_S + 1
        h.login(client)
        _expect_error(h.run_generation(client, alpha_gid, ack=True), 410, "generation_expired")
        _assert_history(client, alpha_history, serving="unavailable")
        _assert_history(client, bravo_history, serving="stale")
        assert h.run_generation(client, bravo_gid, BRAVO, ack=True).status_code == 200
        assert _generation_bytes(h, alpha_gid) == evidence
        calls = len(h.calls)

    with h.generation_client() as client:
        _expect_error(client.get(alpha_history.review_path, headers=old_cookie), 401, "owner_required")
        h.login(client)
        _assert_history(client, alpha_history, serving="unavailable")
        _assert_history(client, bravo_history, serving="stale")
        assert len(h.calls) == calls

    with closing(sqlite3.connect(h.db)) as db:
        audit = db.execute("SELECT code, attempt_id FROM audit ORDER BY sequence").fetchall()
        attempts = {row[0] for row in db.execute("SELECT attempt_id FROM attempt")}
    original = first["attempt"]["attempt_id"]
    assert [row for row in audit if row[0] == "generation_selected"] == [("generation_selected", original)]
    assert ("cached_generation_selected", original) in audit and ("generation_rolled_back", original) in audit
    assert ("forbidden", failed_attempt) in audit and ("owner_required", None) in audit
    assert all(attempt is None or attempt in attempts for _, attempt in audit)
    clinical_audit = []
    for gid in (alpha_gid, bravo_gid):
        with closing(sqlite3.connect(_clinical_db(h, gid))) as db:
            clinical_audit.extend(db.execute("SELECT * FROM audit_event").fetchall())
    assert clinical_audit
    assertion = parse_qs(h.calls[0].content.decode())["client_assertion"][0]
    logs = caplog.text + json.dumps([audit, clinical_audit, fenced, bravo])
    assert all(
        value not in logs
        for value in (
            *secrets,
            assertion,
            BODY,
            BEARER,
            QUESTION,
            PATIENT_A,
            PATIENT_B,
            settings.FHIR_BASE,
            "invented-code",
        )
    )
