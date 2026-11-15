"""Offline contracts for the hosted LIVE Beta backend. All keys, tokens, owners, patients and transports are invented.

No test reaches Epic or Cloudflare: Epic is the existing in-process ``FakeEpic`` and the Access certificate endpoint is
an ``httpx.MockTransport``. A fake transport always yields a ``synthetic_transport`` candidate, so the real gates must
refuse it; the one full round trip below patches those gates explicitly and asserts the fixture label survives.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi.testclient import TestClient

from api.beta_hosted import hosted
from api.beta_hosted.access import AccessPolicy
from api.beta_hosted.hosted_keys import HostedKeyError, load_pem_key, take_key
from api.beta_live_record import live_record_view as real_live_record_view
from src.interop.epic import beta_live_staging as files
from src.interop.epic import settings
from src.interop.epic.beta_acquisition import Limits
from src.interop.epic.keys import key_id, public_jwk
from tests.test_beta_live_generations import FixtureLive
from tests.test_beta_live_route import LIVE_ALIAS, LIVE_PATIENT, FakeEpic

TEAM = "https://invented-team.cloudflareaccess.com"
AUD = "a" * 64
OWNER = "owner@invented.example"
ORIGIN = "https://clinical-trace-ai-beta-live.invented.workers.dev"
KID = "invented-access-kid"
NOW = 1_799_000_000.0
POST_HEADERS = {"origin": ORIGIN, "x-beta-client": "live"}


class Clock:
    now = NOW

    def __call__(self) -> float:
        return self.now


@pytest.fixture(scope="module")
def access_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def epic_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def mint(key, *, header=None, **overrides) -> str:
    claims = {
        "aud": [AUD],
        "iss": TEAM,
        "email": OWNER,
        "type": "app",
        "iat": NOW - 10,
        "nbf": NOW - 10,
        "exp": NOW + 3600,
        "sub": "invented-subject",
    }
    claims.update(overrides)
    claims = {name: value for name, value in claims.items() if value is not None}
    head = {"alg": "RS256", "kid": KID, "typ": "JWT"} | (header or {})
    signing = f"{_b64(json.dumps(head).encode())}.{_b64(json.dumps(claims).encode())}"
    signature = key.sign(signing.encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{signing}.{_b64(signature)}"


class World:
    def __init__(self, tmp_path, access_key, epic_key, monkeypatch):
        self.clock = Clock()
        self.epic = FakeEpic()
        self.cert_calls = 0
        jwk = public_jwk(access_key) | {"kid": KID, "alg": "RS256"}

        def certs(request):
            self.cert_calls += 1
            assert str(request.url) == TEAM + "/cdn-cgi/access/certs"
            return httpx.Response(200, json={"keys": [jwk], "public_cert": {"kid": KID}})

        self.config = hosted.HostedConfig(
            AccessPolicy(TEAM, AUD, OWNER), ORIGIN, LIVE_ALIAS, LIVE_PATIENT, "invented-client-0001", None,
            tmp_path / "hosted-data",
        )
        self.app = hosted.create_hosted_app(
            self.config,
            epic_key,
            clock=self.clock,
            limits=Limits(cooldown_s=0),
            access_transport=httpx.MockTransport(certs),
            epic_transport=httpx.MockTransport(self.epic.respond),
        )
        self.token = mint(access_key)
        self.access_key = access_key

    def get(self, client, path, token=True, **kwargs):
        headers = {"x-beta-client": "live"} | kwargs.pop("headers", {})
        headers = {name: value for name, value in headers.items() if value is not None}
        if token:
            headers["cf-access-jwt-assertion"] = self.token if token is True else token
        return client.get(path, headers=headers, **kwargs)

    def post(self, client, path, token=True, headers=None, **kwargs):
        merged = dict(POST_HEADERS)
        merged.update(headers or {})
        if token:
            merged["cf-access-jwt-assertion"] = self.token if token is True else token
        return client.post(path, headers={k: v for k, v in merged.items() if v is not None}, **kwargs)


@pytest.fixture
def world(tmp_path, access_key, epic_key, monkeypatch):
    monkeypatch.setenv(settings.ENABLE_ENV, "1")
    if sys.platform == "win32":
        # Deliberately fake ACL boundary on Windows; real ACL/SID calls are forbidden by the subprocess guard.
        monkeypatch.setattr(files, "acl_problem", lambda path: None)
    return World(tmp_path, access_key, epic_key, monkeypatch)


def client(world):
    return TestClient(world.app, base_url="https://beta-live.invalid")


def code(response):
    return response.json()["error"]["code"]


def genuine_gates_patched(monkeypatch):
    """Exercise the full path offline: FixtureLive labels fake data LIVE-tier, and the display path accepts it
    only as an explicitly labelled synthetic fixture (never as Epic data)."""
    monkeypatch.setattr(hosted, "LiveAcquisition", FixtureLive)

    def labelled_fixture(manifest):
        assert manifest["synthetic_transport"] is True, "fixture must never masquerade as provider evidence"

    def fixture_view(generations, alias, *, authorized, genuine_only):
        assert genuine_only is True
        return real_live_record_view(generations, alias, authorized=authorized, synthetic_only=True)

    monkeypatch.setattr(hosted, "_require_genuine", labelled_fixture)
    monkeypatch.setattr(hosted, "live_record_view", fixture_view)


# -- configuration and keys ------------------------------------------------------------------------------------


def test_config_from_env_requires_every_value_and_rejects_bad_shapes(tmp_path):
    env = {
        "BETA_ACCESS_TEAM_DOMAIN": TEAM,
        "BETA_ACCESS_AUD": AUD,
        "BETA_OWNER_EMAIL": OWNER,
        "BETA_ORIGIN": ORIGIN,
        "BETA_PATIENT_ALIAS": LIVE_ALIAS,
        "BETA_PATIENT_ID": LIVE_PATIENT,
        "EPIC_CLIENT_ID": "invented-client-0001",
        "BETA_DATA_ROOT": str(tmp_path.resolve()),
    }
    config = hosted.HostedConfig.from_env(env)
    assert (config.alias, config.patient, config.jku) == (LIVE_ALIAS, LIVE_PATIENT, None)
    assert "invented-client" not in repr(config)
    for name in ("BETA_ACCESS_TEAM_DOMAIN", "BETA_ACCESS_AUD", "BETA_OWNER_EMAIL", "BETA_ORIGIN", "BETA_PATIENT_ID"):
        with pytest.raises(ValueError):
            hosted.HostedConfig.from_env({k: v for k, v in env.items() if k != name})
    for name, value in (
        ("BETA_ACCESS_TEAM_DOMAIN", "https://evil.example.com"),
        ("BETA_ACCESS_AUD", "short"),
        ("BETA_ORIGIN", "http://clinical-trace-ai-beta-live.invented.workers.dev"),
        ("BETA_PATIENT_ALIAS", "mock-alpha"),
        ("BETA_PATIENT_ID", "../x"),
        ("EPIC_CLIENT_ID", "bad id"),
        ("EPIC_JKU", "http://keys.invalid/jwks"),
        ("BETA_DATA_ROOT", "relative/data"),
    ):
        with pytest.raises(ValueError):
            hosted.HostedConfig.from_env(env | {name: value})


def test_hosted_key_loads_only_the_expected_unencrypted_rsa_key(epic_key):
    pem = epic_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    kid = key_id(epic_key)
    assert key_id(load_pem_key(pem, kid)) == kid
    env = {"EPIC_PRIVATE_KEY_PEM": pem.decode().replace("\n", "\\n"), "EPIC_KEY_ID": kid}
    assert key_id(take_key(env)) == kid
    assert "EPIC_PRIVATE_KEY_PEM" not in env  # removed from the environment after loading
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(HostedKeyError):
        load_pem_key(pem, key_id(other))
    encrypted = epic_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.BestAvailableEncryption(b"x" * 16)
    )
    with pytest.raises(HostedKeyError):
        load_pem_key(encrypted, kid)
    with pytest.raises(HostedKeyError):
        load_pem_key(b"", kid)
    bad = {"EPIC_PRIVATE_KEY_PEM": "not a key", "EPIC_KEY_ID": kid}
    with pytest.raises(HostedKeyError) as error:
        take_key(bad)
    assert "not a key" not in str(error.value) and "EPIC_PRIVATE_KEY_PEM" not in bad


def test_factory_refuses_a_real_transport_substitute_and_small_keys(world, epic_key):
    with pytest.raises(ValueError):
        hosted.create_hosted_app(world.config, epic_key, epic_transport=httpx.AsyncHTTPTransport())
    with pytest.raises(ValueError):
        hosted.create_hosted_app(world.config, rsa.generate_private_key(public_exponent=65537, key_size=1024))


# -- authentication boundary ----------------------------------------------------------------------------------


def test_health_is_open_and_content_free(world):
    with client(world) as c:
        response = c.get("/beta/api/health")
        assert response.status_code == 200 and response.json() == {"status": "ok"}
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["content-security-policy"].startswith("default-src 'none'")
    assert world.epic.calls == []


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "duplicate",
        "garbage",
        "wrong_audience",
        "wrong_issuer",
        "expired",
        "future",
        "wrong_owner",
        "service_token",
        "no_email",
        "alg_none",
        "hs256",
        "unknown_kid",
        "forged_signature",
    ],
)
def test_every_protected_route_requires_a_valid_owner_access_token(world, change):
    key = world.access_key
    forged = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    tokens = {
        "garbage": "a.b.c",
        "wrong_audience": mint(key, aud=["b" * 64]),
        "wrong_issuer": mint(key, iss="https://other-team.cloudflareaccess.com"),
        "expired": mint(key, exp=NOW - 1),
        "future": mint(key, iat=NOW + 600, nbf=NOW + 600),
        "wrong_owner": mint(key, email="someone@invented.example"),
        "service_token": mint(key, email=None, common_name="invented.access", type="app"),
        "no_email": mint(key, email=None),
        "alg_none": mint(key, header={"alg": "none"}),
        "hs256": mint(key, header={"alg": "HS256"}),
        "unknown_kid": mint(key, header={"kid": "other-kid"}),
        "forged_signature": mint(forged),
    }
    with client(world) as c:
        for method, path in (
            ("GET", "/beta/api/session"),
            ("GET", "/beta/api/status"),
            ("GET", "/beta/api/record"),
            ("POST", "/beta/api/refresh"),
            ("POST", "/beta/api/approve"),
            ("POST", "/beta/api/erase"),
        ):
            headers = dict(POST_HEADERS) if method == "POST" else {}
            if change == "duplicate":
                headers = [*headers.items(), ("cf-access-jwt-assertion", world.token), ("cf-access-jwt-assertion", world.token)]
            elif change != "missing":
                headers["cf-access-jwt-assertion"] = tokens[change]
            response = c.request(method, path, headers=headers)
            assert response.status_code == 401 and code(response) == "owner_required", (change, path)
    assert world.epic.calls == []


def test_owner_session_reports_mode_without_identity(world):
    with client(world) as c:
        body = world.get(c, "/beta/api/session").json()
    assert body["owner"] is True and body["epic_enabled"] is True and body["patient"] == {"alias": LIVE_ALIAS}
    assert OWNER not in json.dumps(body)


def test_access_keys_are_cached_and_refetched_on_rotation(world):
    with client(world) as c:
        for _ in range(3):
            assert world.get(c, "/beta/api/session").status_code == 200
        assert world.cert_calls == 1
        world.clock.now += 601
        world.token = mint(world.access_key, iat=world.clock.now - 1, nbf=world.clock.now - 1, exp=world.clock.now + 60)
        assert world.get(c, "/beta/api/session").status_code == 200
        assert world.cert_calls == 2


@pytest.mark.parametrize(
    "headers",
    [
        {"origin": None},
        {"origin": "https://evil.invented.workers.dev"},
        {"x-beta-client": None},
        {"x-beta-client": "fixture"},
    ],
)
def test_state_changing_routes_require_same_origin_and_the_client_header(world, headers):
    with client(world) as c:
        for path in ("/beta/api/refresh", "/beta/api/approve", "/beta/api/erase"):
            response = world.post(c, path, headers=headers)
            assert response.status_code == 403 and code(response) == "same_origin_required"
    assert world.epic.calls == []


def test_reads_also_require_the_client_header(world):
    """A cross-site navigation or simple request cannot set it, so it cannot even reach the record."""
    with client(world) as c:
        for path in ("/beta/api/session", "/beta/api/status", "/beta/api/record"):
            response = world.get(c, path, headers={"x-beta-client": None})
            assert response.status_code == 403 and code(response) == "same_origin_required"


@pytest.mark.parametrize("email", ["OWNER@INVENTED.EXAMPLE", "owner@invented.example"])
def test_owner_email_matches_ascii_case_insensitively(world, email):
    with client(world) as c:
        assert world.get(c, "/beta/api/session", token=mint(world.access_key, email=email)).status_code == 200


@pytest.mark.parametrize("email", ["owner@invented.eKample", "owner@invented.example ", "ownér@invented.example"])
def test_unicode_lookalike_or_padded_emails_are_refused(world, email):
    with client(world) as c:
        assert world.get(c, "/beta/api/session", token=mint(world.access_key, email=email)).status_code == 401


def test_chunked_post_bodies_are_accepted_within_the_limit(world, monkeypatch):
    genuine_gates_patched(monkeypatch)
    with client(world) as c:
        candidate = world.post(c, "/beta/api/refresh").json()["candidate"]
        payload = json.dumps({"attempt_id": candidate["attempt_id"], "manifest_sha256": candidate["manifest_sha256"]}).encode()
        response = world.post(c, "/beta/api/approve", headers={"content-type": "application/json"}, content=iter([payload]))
        assert response.status_code == 200, response.text
        too_big = world.post(c, "/beta/api/approve", headers={"content-type": "application/json"}, content=iter([b" " * 5000]))
        assert too_big.status_code == 413


def test_unknown_routes_methods_and_queries_are_refused(world):
    with client(world) as c:
        assert world.get(c, "/beta/api/patients").status_code == 404
        assert world.get(c, "/beta/epic/patients").status_code == 404
        assert world.get(c, "/beta/api/refresh").status_code == 405
        assert world.get(c, "/beta/api/record?patient=other").status_code == 400
        assert world.post(c, "/beta/api/refresh", content=b"{}").status_code == 400


# -- kill switch, allowlist and genuine-only gates ------------------------------------------------------------


def test_kill_switch_stops_refresh_before_any_epic_request(world, monkeypatch):
    monkeypatch.delenv(settings.ENABLE_ENV)
    with client(world) as c:
        response = world.post(c, "/beta/api/refresh")
        assert response.status_code == 503 and code(response) == "live_disabled"
        assert world.get(c, "/beta/api/session").json()["epic_enabled"] is False
    assert world.epic.calls == []


def test_record_is_unavailable_before_any_approved_retrieval(world):
    with client(world) as c:
        response = world.get(c, "/beta/api/record")
    assert response.status_code == 404 and code(response) == "generation_unavailable"


def test_fake_transport_data_is_refused_by_the_tier_gate(world):
    """Unpatched: the MockTransport candidate is MOCK-tier and is never staged."""
    with client(world) as c:
        response = world.post(c, "/beta/api/refresh")
        assert response.status_code == 409 and code(response) == "candidate_integrity"
        status = world.get(c, "/beta/api/status").json()
        assert status["candidate"] is None and status["attempt"]["state"] == "failed"


def test_fake_transport_data_is_refused_by_the_genuine_gate(world, monkeypatch):
    """Even labelled LIVE-tier, synthetic-transport evidence cannot be staged for display."""
    monkeypatch.setattr(hosted, "LiveAcquisition", FixtureLive)
    with client(world) as c:
        response = world.post(c, "/beta/api/refresh")
        assert response.status_code == 409 and code(response) == "genuine_candidate_required"
        assert world.get(c, "/beta/api/status").json()["candidate"] is None


def test_only_the_allowlisted_patient_and_eight_read_operations_reach_epic(world, monkeypatch):
    genuine_gates_patched(monkeypatch)
    with client(world) as c:
        assert world.post(c, "/beta/api/refresh").status_code == 200
    assert world.epic.calls and all(
        call.method == "POST" and str(call.url) == settings.TOKEN_URL
        or call.method == "GET" and str(call.url).startswith(settings.FHIR_BASE + "/")
        for call in world.epic.calls
    )
    reads = [call for call in world.epic.calls if call.method == "GET"]
    assert all(
        call.url.path.endswith(f"/Patient/{LIVE_PATIENT}") or call.url.params["patient"] == LIVE_PATIENT for call in reads
    )
    assert {call.url.path.rsplit("/", 1)[1] for call in reads} <= {
        LIVE_PATIENT, "Encounter", "Condition", "Observation", "DocumentReference", "DiagnosticReport",
    }


# -- full offline round trip (gates explicitly patched; the fixture label must survive) ------------------------


def test_refresh_review_approve_and_record_round_trip(world, monkeypatch):
    genuine_gates_patched(monkeypatch)
    with client(world) as c:
        refreshed = world.post(c, "/beta/api/refresh")
        assert refreshed.status_code == 200, refreshed.text
        body = refreshed.json()
        candidate = body["candidate"]
        assert body["synthetic_transport"] is True
        assert body["review"]["patient_display_name"]
        assert body["review"]["http_outcomes"]["OAuth token"] == [200]
        assert body["status"]["candidate"]["manifest_sha256"] == candidate["manifest_sha256"]
        assert world.get(c, "/beta/api/record").status_code == 404  # staged is not displayed before approval

        wrong = world.post(c, "/beta/api/approve", json={"attempt_id": candidate["attempt_id"], "manifest_sha256": "0" * 64})
        assert wrong.status_code == 409
        approved = world.post(
            c, "/beta/api/approve",
            json={"attempt_id": candidate["attempt_id"], "manifest_sha256": candidate["manifest_sha256"]},
        )
        assert approved.status_code == 200, approved.text
        view = approved.json()
        assert view == world.get(c, "/beta/api/record").json()

    assert view["mode"] == "LIVE" and view["alias"] == LIVE_ALIAS
    assert view["data_origin"] == "synthetic_live_fixture" and view["label"] == "Synthetic LIVE-shaped fixture"
    assert view["serving"] == view["freshness"] == "fresh" and view["publication"] == "disabled"
    assert view["provenance"]["generation_id"] == view["active"]["generation_id"] == candidate["generation_id"]
    assert view["provenance"]["manifest_sha256"] == candidate["manifest_sha256"]
    assert view["provenance"]["bundle_sha256"] == candidate["bundle_sha256"]
    codes = [warning["code"] for warning in view["warnings"]]
    assert codes[0] == "hosted_owner_only" and "synthetic_live_fixture" in codes and "clinical_ai_disabled" in codes
    assert "live_access_disabled" not in codes and view["completeness"] == "unconfirmed"
    assert view["record"]["patient"] and all(name in view["record"] for name in ("conditions", "labs", "notes"))


def test_approve_needs_the_exact_payload_and_json(world, monkeypatch):
    genuine_gates_patched(monkeypatch)
    with client(world) as c:
        candidate = world.post(c, "/beta/api/refresh").json()["candidate"]
        good = {"attempt_id": candidate["attempt_id"], "manifest_sha256": candidate["manifest_sha256"]}
        assert world.post(c, "/beta/api/approve", json=good | {"extra": 1}).status_code == 400
        assert world.post(c, "/beta/api/approve", content=json.dumps(good)).status_code == 415
        assert world.post(c, "/beta/api/approve", json={**good, "attempt_id": "x"}).status_code == 400
        unknown = world.post(c, "/beta/api/approve", json={**good, "attempt_id": "u" * 32})
        assert unknown.status_code in (404, 409)
        assert world.post(c, "/beta/api/approve", json=good).status_code == 200


def test_erase_removes_the_candidate_and_the_generation(world, monkeypatch):
    genuine_gates_patched(monkeypatch)
    with client(world) as c:
        candidate = world.post(c, "/beta/api/refresh").json()["candidate"]
        world.post(c, "/beta/api/approve",
                   json={"attempt_id": candidate["attempt_id"], "manifest_sha256": candidate["manifest_sha256"]})
        assert world.get(c, "/beta/api/record").status_code == 200
        assert world.post(c, "/beta/api/erase").status_code == 204
        assert world.get(c, "/beta/api/record").status_code == 404
        status = world.get(c, "/beta/api/status").json()
        assert status["candidate"] is None and status["generation"] is None


def test_catalog_budgets_still_apply(world, monkeypatch):
    genuine_gates_patched(monkeypatch)
    world.app.state.hosted.limits = Limits()  # the real defaults: 5-minute cooldown, 2 per patient per hour
    with client(world) as c:
        assert world.post(c, "/beta/api/refresh").status_code == 200
        limited = world.post(c, "/beta/api/refresh")
        assert limited.status_code == 429 and code(limited) == "quota_exhausted" and int(limited.headers["retry-after"]) > 0


def test_expired_access_session_cannot_promote(world, monkeypatch):
    genuine_gates_patched(monkeypatch)
    with client(world) as c:
        candidate = world.post(c, "/beta/api/refresh").json()["candidate"]
        world.clock.now += 3601
        response = world.post(c, "/beta/api/approve",
                              json={"attempt_id": candidate["attempt_id"], "manifest_sha256": candidate["manifest_sha256"]})
        assert response.status_code == 401


def test_errors_never_echo_tokens_keys_or_identity(world, monkeypatch):
    genuine_gates_patched(monkeypatch)
    with client(world) as c:
        responses = [
            world.post(c, "/beta/api/refresh"),
            world.get(c, "/beta/api/status"),
            world.get(c, "/beta/api/record", token=mint(world.access_key, email="x@invented.example")),
        ]
    text = " ".join(response.text for response in responses)
    assert OWNER not in text and world.token not in text and "invented-live-route-token" not in text
    assert "invented-client-0001" not in text


def test_review_matches_the_owner_coordinator(world, monkeypatch):
    pytest.importorskip("fido2")
    if sys.platform != "win32" or sys.version_info < (3, 13):
        pytest.skip("the owner coordinator imports the Windows-only owner stack")
    from api import beta_owner_coordinator

    genuine_gates_patched(monkeypatch)
    with client(world) as c:
        candidate = world.post(c, "/beta/api/refresh").json()["candidate"]
        staging = world.app.state.hosted.staging
        # The catalog's SQLite connection belongs to the app's thread; read through it, not the test thread.
        _, raw, bundle = c.portal.call(staging.read_candidate, candidate["attempt_id"], LIVE_ALIAS)
    manifest = json.loads(raw)
    assert hosted.review_candidate(manifest, bundle, LIVE_PATIENT) == beta_owner_coordinator._review_candidate(
        manifest, bundle, LIVE_PATIENT
    )


def test_hosted_modules_do_not_import_the_owner_stack_or_llm_runtime():
    imports = [
        line.strip()
        for name in ("hosted.py", "access.py", "hosted_keys.py")
        for line in (Path(hosted.__file__).parent / name).read_text(encoding="utf-8").splitlines()
        if line.strip().startswith(("import ", "from "))
    ]
    for forbidden in ("beta_owner", "beta_webauthn", "fido2", "langchain", "src.agent", "load_private_key"):
        assert not any(forbidden in line for line in imports), forbidden
