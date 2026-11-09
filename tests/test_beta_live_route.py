"""Offline contract for the owner route's staging-only LIVE refresh and its private candidate lifecycle.

A fake Epic stands in for the sandbox: no real request, credential or LIVE patient is involved.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from api.beta import create_mock_beta_app
from src.interop.epic import beta_catalog, settings
from src.interop.epic.beta_acquisition import MOCK_SCOPES
from src.interop.epic.beta_catalog import Catalog
from src.interop.epic.beta_live_acquisition import LiveRefresh
from src.interop.epic.beta_live_staging import STAGING_RETENTION_S, LiveAllowlist
from tests.test_beta_generations import BODY, fixture_resources
from tests.test_beta_owner_refresh import ALPHA, ORIGIN, harness as owner_fixture, keys as keys_fixture

harness = owner_fixture
keys = keys_fixture

LIVE_ALIAS = "approved-alpha"
LIVE_PATIENT = "invented-live-alpha"
LIVE = f"/beta/epic/patients/{LIVE_ALIAS}"
LIVE_TOKEN = "invented-live-route-token"
EIGHT = {
    "Patient",
    "Encounter",
    "Condition:problem-list-item",
    "Condition:encounter-diagnosis",
    "Observation:laboratory",
    "Observation:vital-signs",
    "DocumentReference:clinical-note",
    "DiagnosticReport",
}


class FakeEpic:
    """Stands in for the LIVE sandbox; ``outcomes`` injects warning-only search-outcome entries by operation."""

    def __init__(self):
        self.calls = []
        self.records = fixture_resources(LIVE_PATIENT)
        self.outcomes = {}
        self.scopes = " ".join(sorted(MOCK_SCOPES))
        self.hold = False

    async def respond(self, request):
        self.calls.append(request)
        if request.method == "POST":
            return httpx.Response(
                200, json={"access_token": LIVE_TOKEN, "token_type": "Bearer", "expires_in": 300, "scope": self.scopes}
            )
        if self.hold:
            await asyncio.sleep(10)
        assert request.headers["Authorization"] == f"Bearer {LIVE_TOKEN}"
        if request.url.path == settings.FHIR_BASE_PATH + f"/Patient/{LIVE_PATIENT}":
            return httpx.Response(200, json=self.records[0])
        kind = request.url.path.rsplit("/", 1)[1]
        assert request.url.params["patient"] == LIVE_PATIENT
        category = request.url.params.get("category")
        selected = [r for r in self.records if r["resourceType"] == kind]
        if category:
            selected = [
                r
                for r in selected
                if any(coding["code"] == category for concept in r["category"] for coding in concept["coding"])
            ]
        entries = [{"resource": r} for r in selected]
        code = self.outcomes.get(f"{kind}:{category}" if category else kind)
        if code:
            outcome = {
                "resourceType": "OperationOutcome",
                "issue": [{"severity": "warning", "code": "informational", "details": {"coding": [{"code": code}]}}],
            }
            entries.append({"search": {"mode": "outcome"}, "resource": outcome})
        return httpx.Response(
            200, json={"resourceType": "Bundle", "type": "searchset", "total": len(selected), "entry": entries}
        )


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setenv(settings.ENABLE_ENV, "1")
    return FakeEpic()


def allowlist():
    return LiveAllowlist({LIVE_ALIAS: LIVE_PATIENT})


def build(harness, keys, fake, *, live=True, approved=None, db=None, identity=None):
    refresh = (
        LiveRefresh(
            key=keys[1],
            config=settings.EpicConfig("invented-live-client", Path("never-read.key"), "https://keys.invalid/jwks"),
            allowlist=approved if approved is not None else allowlist(),
            transport=httpx.MockTransport(fake.respond),
        )
        if live
        else None
    )
    app = create_mock_beta_app(
        db or harness.db,
        oidc_key=keys[0].public_key(),
        epic_key=keys[1],
        oidc_transport=httpx.MockTransport(harness.oidc),
        epic_transport=httpx.MockTransport(harness.epic),
        limits=harness.limits,
        clock=lambda: harness.clock,
        live=refresh,
        **({"identity": identity} if identity is not None else {}),
    )
    return TestClient(app, base_url=ORIGIN, follow_redirects=False)


def staging_dir(harness):
    return harness.db.parent / "live-staging"


def staged(harness):
    root = staging_dir(harness)
    return sorted(p.name for p in root.iterdir()) if root.exists() else []


def refresh_and_wait(harness, client, key="test-key-12345"):
    assert harness.post(client, LIVE + "/refresh", key=key).status_code == 202
    return harness.wait(client, LIVE).json()


def begin_step_up(harness, client):
    """Start the owner's reauthentication; returns the flow state the invented IdP would echo back."""
    start = harness.post(client, "/beta/auth/step-up")
    assert start.status_code == 303
    target = urlsplit(start.headers["location"])
    harness.auth_request = parse_qs(target.query)
    harness.step_up_request = harness.auth_request
    return harness.auth_request["state"][0]


def step_up(harness, client, **claims):
    """A complete, valid step-up: a new MFA authentication happening now. Extra claims override the defaults."""
    state = begin_step_up(harness, client)
    harness.claims = {"auth_time": int(harness.clock), "acr": "mfa", "amr": ["pwd", "mfa"], **claims}
    try:
        return harness.finish(client, state)
    finally:
        harness.claims = {}


def test_live_refresh_stages_privately_without_publication(harness, keys, fake):
    fake.outcomes = {"Condition:encounter-diagnosis": "4101", "DocumentReference:clinical-note": "59100"}
    with build(harness, keys, fake) as client:
        harness.login(client)
        body = refresh_and_wait(harness, client)
        assert (body["mode"], body["source"]) == ("LIVE", settings.SOURCE_SYSTEM)
        assert (body["serving"], body["publication"]) == ("unavailable", "disabled")
        attempt = body["attempt"]
        assert attempt["state"] == "succeeded" and attempt["policy"] == beta_catalog.LIVE_POLICY_VERSION
        assert attempt["requests"] == 9 and len(fake.calls) == 9
        operations = attempt["operations"]
        assert {n for n, o in operations.items() if o["execution"] == "succeeded"} == EIGHT
        assert operations["MedicationRequest"]["execution"] == operations["Binary"]["execution"] == "not_requested"
        coded = {name: o["informational_codes"] for name, o in operations.items() if "informational_codes" in o}
        assert coded == {"Condition:encounter-diagnosis": ["4101"], "DocumentReference:clinical-note": ["59100"]}
        assert operations["Condition:encounter-diagnosis"]["matches"] == 1  # outcome entries are never matches
        searches = [r for r in fake.calls if r.method == "GET" and r.url.query]
        assert len(searches) == 7 and all(set(r.url.params) <= {"patient", "category"} for r in searches)
        assert harness.calls == []  # the MOCK Epic transport is never used for a LIVE alias
        # A private candidate exists, is described without content, and cannot be served or run against.
        candidate = body["candidate"]
        assert set(candidate) == {
            "attempt_id",
            "state",
            "bytes",
            "created_at",
            "expires_at",
            "manifest_sha256",
            "review",
        }
        assert candidate["state"] == "staged" and candidate["attempt_id"] == attempt["attempt_id"]
        assert candidate["expires_at"] - candidate["created_at"] == STAGING_RETENTION_S
        assert staged(harness) == [attempt["attempt_id"]]
        files = {p.name: p.read_bytes() for p in (staging_dir(harness) / attempt["attempt_id"]).iterdir()}
        assert set(files) == {"manifest.json", "normalized_bundle.json"}
        assert all(BODY.encode() not in data and b"Binary/secret" not in data for data in files.values())
        assert client.get(f"{LIVE}/generations/g/runs/r").status_code == 404
        for text in (client.get(LIVE + "/status").text, harness.db.read_bytes().decode(errors="ignore")):
            assert all(secret not in text for secret in (LIVE_PATIENT, LIVE_TOKEN, BODY, "invented-code"))


@pytest.mark.parametrize("case", ["no_live_argument", "empty_allowlist"])
def test_live_alias_is_unreachable_by_default(harness, keys, fake, case):
    approved = LiveAllowlist.empty() if case == "empty_allowlist" else None
    with build(harness, keys, fake, live=case != "no_live_argument", approved=approved) as client:
        harness.login(client)
        assert client.get(LIVE + "/status").status_code == 404
        for action in ("/refresh", "/revoke", "/candidate/erase"):
            assert harness.post(client, LIVE + action).status_code == 404
    assert fake.calls == [] and harness.calls == []
    assert not staging_dir(harness).exists() or staged(harness) == []


def test_disabled_flag_fails_the_attempt_without_a_request_and_does_not_leave_it_busy(harness, keys, fake, monkeypatch):
    monkeypatch.delenv(settings.ENABLE_ENV)
    with build(harness, keys, fake) as client:
        harness.login(client)
        attempt = refresh_and_wait(harness, client)["attempt"]
        assert (attempt["state"], attempt["error_code"]) == ("failed", "live_disabled")
        assert fake.calls == [] and staged(harness) == []
        monkeypatch.setenv(settings.ENABLE_ENV, "1")
        harness.clock += 400  # past the per-patient cooldown; a stuck queued attempt would answer refresh_busy
        assert refresh_and_wait(harness, client, key="second-key-12345")["attempt"]["state"] == "succeeded"


def test_unapproved_grant_is_a_failed_attempt_and_stages_nothing(harness, keys, fake):
    fake.scopes = " ".join(sorted({*MOCK_SCOPES, "system/Patient.write"}))
    with build(harness, keys, fake) as client:
        harness.login(client)
        body = refresh_and_wait(harness, client)
        assert (body["attempt"]["state"], body["attempt"]["error_code"]) == ("failed", "unapproved_grant")
        assert len(fake.calls) == 1 and body["candidate"] is None and staged(harness) == []


def test_only_refresh_status_revoke_and_erase_know_a_live_alias(harness, keys, fake):
    with build(harness, keys, fake) as client:
        harness.login(client)
        for path in ("/generations/g/select", "/generations/g/rollback", "/generations/g/runs"):
            assert harness.post(client, LIVE + path).status_code == 404
        assert client.get(LIVE + "/generations/g/reviews/r").status_code == 404
        assert harness.post(client, ALPHA + "/candidate/erase").status_code == 404  # MOCK aliases have none
    assert fake.calls == []


def test_owner_requested_erasure_removes_the_candidate_and_its_files(harness, keys, fake):
    with build(harness, keys, fake) as client:
        harness.login(client)
        refresh_and_wait(harness, client)
        assert len(staged(harness)) == 1
        assert harness.post(client, LIVE + "/candidate/erase").status_code == 204
        assert staged(harness) == []
        assert client.get(LIVE + "/status").json()["candidate"] is None
        assert harness.post(client, LIVE + "/candidate/erase").status_code == 204  # idempotent


def test_patient_revocation_erases_the_candidate_and_blocks_the_alias(harness, keys, fake):
    with build(harness, keys, fake) as client:
        harness.login(client)
        refresh_and_wait(harness, client)
        assert len(staged(harness)) == 1
        assert harness.post(client, LIVE + "/revoke").status_code == 204
        assert staged(harness) == []
        assert client.get(LIVE + "/status").status_code == 404
        assert harness.post(client, LIVE + "/refresh", key="after-revoke-12345").status_code == 404
    assert len(fake.calls) == 9


def test_owner_revocation_erases_the_candidate(harness, keys, fake):
    with build(harness, keys, fake) as client:
        harness.login(client)
        refresh_and_wait(harness, client)
        assert len(staged(harness)) == 1
        client.portal.call(client.app.state.revoke_owner)
        assert staged(harness) == []
        assert client.get(LIVE + "/status").status_code == 401


@pytest.mark.parametrize("scope", ["patient", "owner", "erase", "logout"])
def test_revocation_while_a_live_refresh_is_in_flight_stages_nothing(harness, keys, fake, scope):
    fake.hold = True
    with build(harness, keys, fake) as client:
        harness.login(client)
        assert harness.post(client, LIVE + "/refresh").status_code == 202
        until = time.monotonic() + 1
        while len(fake.calls) < 2 and time.monotonic() < until:
            time.sleep(0.01)
        assert len(fake.calls) == 2  # token and the first held FHIR request
        if scope == "patient":
            assert harness.post(client, LIVE + "/revoke").status_code == 204
        elif scope == "owner":
            client.portal.call(client.app.state.revoke_owner)
        elif scope == "erase":
            assert harness.post(client, LIVE + "/candidate/erase").status_code == 204
        else:
            assert harness.post(client, "/beta/auth/logout").status_code == 204
        assert staged(harness) == []
        fake.hold = False
        time.sleep(0.2)  # a cancelled task must not stage late
        assert staged(harness) == []
    assert len(fake.calls) == 2


def test_candidate_expires_after_24_hours_and_is_purged_on_the_next_request(harness, keys, fake):
    with build(harness, keys, fake) as client:
        harness.login(client)
        refresh_and_wait(harness, client)
        assert len(staged(harness)) == 1
        harness.clock += STAGING_RETENTION_S + 1
        assert client.get(LIVE + "/status").status_code == 401  # the 8-hour session cap has also passed
        harness.login(client)
        assert client.get(LIVE + "/status").json()["candidate"] is None
        assert staged(harness) == []


def test_restart_keeps_a_fresh_candidate_removes_orphans_and_erases_revoked_ones(harness, keys, fake):
    with build(harness, keys, fake) as client:
        harness.login(client)
        attempt = refresh_and_wait(harness, client)["attempt"]["attempt_id"]
    (staging_dir(harness) / "orphan.tmp").mkdir()
    (staging_dir(harness) / "orphan.tmp" / "normalized_bundle.json").write_bytes(b"stray")
    with build(harness, keys, fake) as client:
        harness.login(client)
        assert staged(harness) == [attempt]  # fresh candidate survives; the orphan is gone
        assert client.get(LIVE + "/status").json()["candidate"]["attempt_id"] == attempt
    catalog = Catalog(harness.db, lambda: harness.clock)
    catalog.revoke(LIVE_ALIAS)  # revoked while the app was down
    catalog.close()
    with build(harness, keys, fake):
        assert staged(harness) == []


def test_staging_root_inside_a_git_work_tree_fails_startup(harness, keys, fake, tmp_path):
    work = tmp_path / "work"
    (work / ".git").mkdir(parents=True)
    with pytest.raises(ValueError, match="Git"):
        with build(harness, keys, fake, db=work / "control.sqlite"):
            pass
    assert fake.calls == []


def test_no_staging_directory_exists_without_live(harness, keys, fake):
    with build(harness, keys, fake, live=False) as client:
        harness.login(client)
        assert harness.post(client, ALPHA + "/refresh").status_code == 202
        harness.wait(client, ALPHA)
    assert not staging_dir(harness).exists()


def test_mock_aliases_are_unchanged_when_live_is_supplied(harness, keys, fake):
    with build(harness, keys, fake) as client:
        harness.login(client)
        assert harness.post(client, ALPHA + "/refresh").status_code == 202
        status = harness.wait(client, ALPHA)
        assert status.json()["mode"] == "MOCK" and status.json()["attempt"]["policy"] == beta_catalog.POLICY_VERSION
        assert status.json()["attempt"]["requests"] == 9 and len(harness.calls) == 9
        assert fake.calls == [] and staged(harness) == []  # the LIVE transport and staging are never used
