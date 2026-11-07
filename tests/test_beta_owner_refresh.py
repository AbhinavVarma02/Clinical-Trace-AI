"""Offline security and acquisition contract for the isolated M2.3-2 app."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi.testclient import TestClient

from src.interop.epic import settings

ORIGIN = "https://beta.invalid"
ISSUER = "https://owner-idp.invalid"
OWNER = "invented-owner"
ALPHA = "/beta/epic/patients/mock-alpha"
BRAVO = "/beta/epic/patients/mock-bravo"
PATIENT_A = "invented-patient-alpha"
PATIENT_B = "invented-patient-bravo"
CONTENT = "invented clinical text - never log"
BEARER = "invented-fhir-bearer"
FHIR_CATEGORY = {
    "problem-list-item": "condition-category",
    "encounter-diagnosis": "condition-category",
    "laboratory": "observation-category",
    "vital-signs": "observation-category",
    "clinical-note": "us-core-documentreference-category",
}


def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def signed(key, claims, alg="RS256"):
    head = b64(json.dumps({"alg": alg, "typ": "JWT"}).encode())
    body = b64(json.dumps(claims).encode())
    signature = key.sign(f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{head}.{body}.{b64(signature)}"


def bundle(*resources, next_link=None):
    return httpx.Response(
        200,
        json={
            "resourceType": "Bundle",
            "type": "searchset",
            "entry": [{"resource": resource} for resource in resources],
            "link": [{"relation": "next", "url": next_link}] if next_link else [],
        },
    )


@pytest.fixture(scope="module")
def keys():
    return tuple(rsa.generate_private_key(public_exponent=65537, key_size=2048) for _ in range(2))


@pytest.fixture
def harness(tmp_path, keys):
    from api.beta import create_mock_beta_app
    from src.interop.epic.beta_acquisition import Limits

    class Harness:
        def __init__(self):
            self.clock = time.time()
            self.calls = []
            self.oidc_calls = []
            self.claims = {}
            self.alg = "RS256"
            self.signer = keys[0]
            self.auth_request = {}
            self.fhir_override = None
            self.token_override = None
            self.db = tmp_path / "control.sqlite"
            self.limits = Limits()
            self.csrf = ""

        def oidc(self, request):
            self.oidc_calls.append(request)
            assert str(request.url) == f"{ISSUER}/token"
            fields = parse_qs(request.content.decode())
            assert fields["grant_type"] == ["authorization_code"]
            assert fields["code"] == ["invented-code"]
            assert fields["redirect_uri"] == [f"{ORIGIN}/beta/auth/callback"]
            assert fields["client_id"] == self.auth_request["client_id"]
            challenge = b64(hashlib.sha256(fields["code_verifier"][0].encode()).digest())
            assert self.auth_request["code_challenge"] == [challenge]
            assert self.auth_request["code_challenge_method"] == ["S256"]
            assert self.auth_request["response_type"] == ["code"]
            assert self.auth_request["scope"] == ["openid"]
            claims = dict(
                iss=ISSUER,
                sub=OWNER,
                aud=fields["client_id"][0],
                nonce=self.auth_request["nonce"][0],
                iat=int(self.clock),
                exp=int(self.clock) + 60,
            )
            claims.update(self.claims)
            return httpx.Response(200, json={"id_token": signed(self.signer, claims, self.alg)})

        async def epic(self, request):
            self.calls.append(request)
            if str(request.url) == settings.TOKEN_URL:
                assert request.method == "POST"
                form = parse_qs(request.content.decode())
                assert set(form) == {"grant_type", "client_assertion", "client_assertion_type"}
                parts = form["client_assertion"][0].split(".")
                keys[1].public_key().verify(
                    base64.urlsafe_b64decode(parts[2] + "=="),
                    ".".join(parts[:2]).encode(),
                    padding.PKCS1v15(),
                    hashes.SHA384(),
                )
                if self.token_override:
                    return await self.token_override(request)
                return httpx.Response(200, json={"access_token": BEARER, "token_type": "Bearer", "expires_in": 300})
            assert request.method == "GET"
            assert request.headers["Authorization"] == f"Bearer {BEARER}"
            if self.fhir_override:
                response = await self.fhir_override(request)
                if response is not None:
                    return response
            path = request.url.path.removeprefix(settings.FHIR_BASE_PATH + "/")
            if path.startswith("Patient/"):
                return httpx.Response(200, json={"resourceType": "Patient", "id": path.split("/")[1], "text": CONTENT})
            patient = request.url.params["patient"]
            category = request.url.params.get("category")
            resource = {
                "resourceType": path,
                "id": f"fixture-{category or path}",
                "subject": {"reference": f"Patient/{patient}"},
            }
            if category:
                resource["category"] = [
                    {
                        "coding": [
                            {
                                "system": f"http://terminology.hl7.org/CodeSystem/{FHIR_CATEGORY[category]}",
                                "code": category,
                            }
                        ],
                    }
                ]
                if path == "DocumentReference":
                    resource["category"][0]["coding"][0]["system"] = (
                        "http://hl7.org/fhir/us/core/CodeSystem/us-core-documentreference-category"
                    )
                    resource["content"] = [
                        {"attachment": {"url": "Binary/invented-secret", "data": b64(CONTENT.encode())}},
                        {"attachment": {"url": "https://evil.invalid/invented-secret"}},
                    ]
            return bundle(resource)

        def client(self):
            app = create_mock_beta_app(
                self.db,
                oidc_key=keys[0].public_key(),
                epic_key=keys[1],
                oidc_transport=httpx.MockTransport(self.oidc),
                epic_transport=httpx.MockTransport(self.epic),
                limits=self.limits,
                clock=lambda: self.clock,
            )
            return TestClient(app, base_url=ORIGIN, follow_redirects=False)

        def begin(self, client):
            start = client.post("/beta/auth/login", headers={"Origin": ORIGIN})
            assert start.status_code == 303
            target = urlsplit(start.headers["location"])
            assert f"{target.scheme}://{target.netloc}{target.path}" == f"{ISSUER}/authorize"
            self.auth_request = parse_qs(target.query)
            return self.auth_request["state"][0]

        def finish(self, client, state):
            return client.get("/beta/auth/callback", params={"state": state, "code": "invented-code"})

        def login(self, client):
            result = self.finish(client, self.begin(client))
            assert result.status_code == 200
            self.csrf = result.json()["csrf_token"]
            assert "__Host-beta-session=" in result.headers["set-cookie"]
            assert "HttpOnly" in result.headers["set-cookie"]
            assert "Secure" in result.headers["set-cookie"]
            assert "SameSite=lax" in result.headers["set-cookie"]
            assert "Domain=" not in result.headers["set-cookie"]
            return result

        def post(self, client, path, key="test-key-12345", **headers):
            return client.post(
                path,
                headers={"Origin": ORIGIN, "X-CSRF-Token": self.csrf, "Idempotency-Key": key, **headers},
            )

        def wait(self, client, target=ALPHA):
            until = time.monotonic() + 5
            while time.monotonic() < until:
                result = client.get(f"{target}/status")
                if result.status_code != 200 or result.json()["attempt"]["state"] not in {"queued", "running"}:
                    return result
                time.sleep(0.01)
            pytest.fail("attempt did not finish")

    return Harness()


@pytest.mark.parametrize(
    "path",
    [
        "/api/v2/system/status",
        "/api/v2/patients",
        "/api/v2/personas",
        "/api/v2/agent/runs",
        "/api/v2/ops/audit",
        "/predict",
        "/fhir/Patient",
        "/health",
        "/openapi.json",
        "/app/",
        ALPHA + "/status",
    ],
)
def test_entire_origin_requires_owner_and_legacy_stays_closed(harness, path):
    with harness.client() as client:
        assert client.get(path).status_code == 401
        if path != ALPHA + "/status":
            harness.login(client)
            assert client.get(path).status_code == 404
    assert not harness.calls


@pytest.mark.parametrize(
    "claim,value",
    [
        ("iss", "https://spoof.invalid"),
        ("aud", "wrong-audience"),
        ("sub", "unknown-owner"),
        ("exp", 1),
        ("nonce", "wrong"),
        ("iat", 9_999_999_999),
    ],
)
def test_oidc_rejects_invalid_claims(harness, claim, value):
    harness.claims[claim] = value
    with harness.client() as client:
        assert harness.finish(client, harness.begin(client)).status_code == 401
        assert client.get(ALPHA + "/status").status_code == 401
    assert not harness.calls


@pytest.mark.parametrize("failure", ["signature", "algorithm", "state", "cookie", "replay", "expired_state"])
def test_oidc_forgery_and_replay(harness, keys, failure):
    with harness.client() as client:
        state = harness.begin(client)
        if failure == "signature":
            harness.signer = keys[1]
        if failure == "algorithm":
            harness.alg = "none"
        if failure == "state":
            state += "wrong"
        if failure == "cookie":
            client.cookies.clear()
        if failure == "replay":
            assert harness.finish(client, state).status_code == 200
        if failure == "expired_state":
            harness.clock += 301
        assert harness.finish(client, state).status_code == 401
        if failure in {"state", "cookie", "expired_state"}:
            assert not harness.oidc_calls
    assert not harness.calls


@pytest.mark.parametrize(
    "header",
    [
        "X-Owner-Id",
        "X-Demo-Persona",
        "X-Demo-Workspace",
        "X-Data-Source",
        "X-Gateway-Token",
        "CF-Access-Jwt-Assertion",
        "Authorization",
        "Forwarded",
        "X-Forwarded-Host",
        "X-Edge-Client",
    ],
)
def test_spoofed_headers_are_rejected_before_outbound(harness, header):
    with harness.client() as client:
        harness.login(client)
        response = harness.post(client, ALPHA + "/refresh", **{header: "forged"})
        assert response.status_code == 400
    assert not harness.calls


@pytest.mark.parametrize("header", [b"X-Owner-Id", b"X-Demo-Persona", b"Authorization", b"X-Forwarded-Host"])
def test_mixed_case_raw_asgi_headers_are_rejected_before_outbound(harness, header):
    refresh_path = ALPHA + "/refresh"

    def with_raw_header(app):
        async def inject(scope, receive, send):
            if scope["type"] == "http" and scope["path"] == refresh_path:
                # Insert after TestClient constructs the scope and lowercases its own headers.
                scope = {**scope, "headers": [*scope["headers"], (header, b"forged")]}
            await app(scope, receive, send)

        return inject

    client = harness.client()
    client.app.add_middleware(with_raw_header)
    with client:
        harness.login(client)
        response = harness.post(client, refresh_path)
        assert response.status_code == 400
        assert response.json() == {"error": {"code": "untrusted_header"}}
        assert client.get(ALPHA + "/status").json()["attempt"] is None
    assert not harness.calls


def test_csrf_origin_cookie_and_revocation(harness):
    with harness.client() as client:
        assert client.post("/beta/auth/login", headers={"Origin": "https://evil.invalid"}).status_code == 403
        harness.login(client)
        assert harness.post(client, ALPHA + "/refresh", Origin="https://evil.invalid").status_code == 403
        assert harness.post(client, ALPHA + "/refresh", **{"X-CSRF-Token": "forged"}).status_code == 403
        assert client.post(ALPHA + "/refresh", headers={"Origin": ORIGIN}).status_code == 403
        cookie = client.cookies.get("__Host-beta-session")
        assert harness.post(client, "/beta/auth/logout").status_code == 204
        client.cookies.set("__Host-beta-session", cookie, path="/")
        assert client.get(ALPHA + "/status").status_code == 401
        client.cookies.clear()
        client.cookies.set("__Host-beta-session", "forged", path="/")
        assert client.get(ALPHA + "/status").status_code == 401
    assert not harness.calls


@pytest.mark.parametrize("idle", [901, 8 * 3600])
def test_session_expiry(harness, idle):
    with harness.client() as client:
        harness.login(client)
        if idle > 901:
            for _ in range(32):
                harness.clock += 899
                assert client.get(ALPHA + "/status").status_code == 200
            harness.clock += idle - 32 * 899
        else:
            harness.clock += idle
        assert harness.post(client, ALPHA + "/refresh").status_code == 401
    assert not harness.calls


def test_rejects_alias_source_query_body_and_revoked_patient(harness):
    with harness.client() as client:
        harness.login(client)
        base = "/beta/epic/patients/"
        for alias in (PATIENT_A, "Mock-alpha", "unlisted", "mock-alpha%2Fextra"):
            assert harness.post(client, base + alias + "/refresh").status_code == 404
        assert harness.post(client, ALPHA + "/refresh?source=live").status_code == 400
        assert (
            client.post(
                ALPHA + "/refresh",
                json={"patient": PATIENT_B, "url": "https://evil.invalid"},
                headers={"Origin": ORIGIN, "X-CSRF-Token": harness.csrf, "Idempotency-Key": "body-test-1234"},
            ).status_code
            == 400
        )
        assert harness.post(client, ALPHA + "/revoke").status_code == 204
        assert client.get(ALPHA + "/status").status_code == 404
        assert harness.post(client, ALPHA + "/refresh").status_code == 404
        assert client.get(BRAVO + "/status").status_code == 200
    assert not harness.calls
    with harness.client() as client:
        harness.login(client)
        assert client.get(ALPHA + "/status").status_code == 404


def test_exact_profile_two_patients_and_no_publication(harness, caplog):
    with harness.client() as client:
        harness.login(client)
        for target, patient in ((ALPHA, PATIENT_A), (BRAVO, PATIENT_B)):
            response = harness.post(client, target + "/refresh", key=f"key-{target}-test")
            assert response.status_code == 202
            status = harness.wait(client, target)
            assert status.headers["Cache-Control"] == "no-store"
            assert status.json()["serving"] == "unavailable"
            attempt = status.json()["attempt"]
            assert attempt["state"] == "succeeded"
            assert attempt["attempt_id"] == response.json()["attempt_id"]
            assert attempt["requests"] == 9
            assert attempt["note_bodies"] == "not_imported"
            calls = harness.calls[-9:]
            assert str(calls[1].url) == f"{settings.FHIR_BASE}/Patient/{patient}"
            assert [(r.url.path.split("/")[-1], r.url.params.get("category")) for r in calls[2:]] == [
                ("Encounter", None),
                ("Condition", "problem-list-item"),
                ("Condition", "encounter-diagnosis"),
                ("Observation", "laboratory"),
                ("Observation", "vital-signs"),
                ("DocumentReference", "clinical-note"),
                ("DiagnosticReport", None),
            ]
            assert all(r.url.params["patient"] == patient for r in calls[2:])
            for text in (status.text, caplog.text, harness.db.read_bytes().decode(errors="ignore")):
                assert all(secret not in text for secret in (PATIENT_A, PATIENT_B, BEARER, CONTENT, "invented-code"))
        assert client.get("/fhir/Patient/" + PATIENT_A).status_code == 404


@pytest.mark.parametrize(
    "path",
    [
        "/MedicationRequest?patient=" + PATIENT_A,
        "/Binary/note?patient=" + PATIENT_A,
        "/Encounter?patient=" + PATIENT_B + "&_cursor=c1",
        "/Encounter?patient=" + PATIENT_A + "&category=laboratory&_cursor=c1",
        "/Encounter?patient=" + PATIENT_A + "&_include=Encounter:subject&_cursor=c1",
        "/Encounter?patient=" + PATIENT_A + "&patient=" + PATIENT_B + "&_cursor=c1",
        "/Encounter?patient=" + PATIENT_A + "&_cursor=%252e",
        "/Encounter%2f..%2fBinary?patient=" + PATIENT_A + "&_cursor=c1",
        "/Encounter?patient=" + PATIENT_A + "&_cursor=c1#fragment",
        "/Encounter?patient=" + PATIENT_A,
        "/Encounter?patient=" + PATIENT_A + "&_cursor=c1&method=POST",
        "https://evil.invalid/Encounter?patient=" + PATIENT_A + "&_cursor=c1",
    ],
)
def test_pagination_scope_checks_precede_dispatch(harness, path):
    async def attack(request):
        if request.url.path.endswith("/Encounter"):
            return bundle(next_link=settings.FHIR_BASE + path if path.startswith("/") else path)
        return None

    harness.fhir_override = attack
    with harness.client() as client:
        harness.login(client)
        assert harness.post(client, ALPHA + "/refresh").status_code == 202
        assert harness.wait(client).json()["attempt"]["error_code"] == "pagination_scope"
    assert len(harness.calls) == 3


def test_valid_pagination_and_empty_terminal_search(harness):
    async def pages(request):
        if request.url.path.endswith("/Encounter"):
            return bundle(next_link=None if "_cursor" in request.url.params else str(request.url) + "&_cursor=c1")
        return None

    harness.fhir_override = pages
    with harness.client() as client:
        harness.login(client)
        harness.post(client, ALPHA + "/refresh")
        attempt = harness.wait(client).json()["attempt"]
        assert attempt["state"] == "succeeded"
        assert attempt["requests"] == 10
        assert attempt["operations"]["Encounter"]["pages"] == 2
        assert attempt["operations"]["Encounter"]["matches"] == 0


@pytest.mark.parametrize(
    "attack,code",
    [
        ("wrong-patient", "patient_scope"),
        ("wrong-read", "patient_scope"),
        ("wrong-type", "resource_scope"),
        ("wrong-category", "resource_scope"),
        ("binary-entry", "resource_scope"),
        ("outcome", "invalid_bundle"),
        ("malformed", "invalid_json"),
        ("invalid-shape", "invalid_bundle"),
        ("oversized", "response_too_large"),
        ("redirect", "redirect_refused"),
    ],
)
def test_untrusted_fhir_responses(harness, attack, code):
    async def reply(request):
        if attack == "wrong-read":
            return httpx.Response(200, json={"resourceType": "Patient", "id": PATIENT_B})
        if attack == "wrong-category":
            if request.url.params.get("category") == "laboratory":
                return bundle(
                    {
                        "resourceType": "Observation",
                        "id": "test",
                        "subject": {"reference": f"Patient/{PATIENT_A}"},
                        "category": [
                            {
                                "coding": [
                                    {
                                        "system": "http://terminology.hl7.org/CodeSystem/observation-category",
                                        "code": "vital-signs",
                                    }
                                ]
                            }
                        ],
                    }
                )
            return None
        if not request.url.path.endswith("/Encounter"):
            return None
        responses = {
            "wrong-patient": lambda: bundle(
                {
                    "resourceType": "Encounter",
                    "id": "test",
                    "subject": {"reference": f"Patient/{PATIENT_B}"},
                }
            ),
            "wrong-type": lambda: bundle({"resourceType": "DiagnosticReport", "id": "test"}),
            "binary-entry": lambda: bundle({"resourceType": "Binary", "id": "test", "data": CONTENT}),
            "outcome": lambda: bundle({"resourceType": "OperationOutcome", "issue": [{"diagnostics": CONTENT}]}),
            "malformed": lambda: httpx.Response(200, content=b"{", headers={"Content-Type": "application/json"}),
            "invalid-shape": lambda: httpx.Response(
                200, json={"resourceType": "Bundle", "type": "searchset", "entry": {}}
            ),
            "oversized": lambda: httpx.Response(200, content=b"X" * 2_000_001),
            "redirect": lambda: httpx.Response(302, headers={"Location": "https://evil.invalid"}),
        }
        return responses[attack]()

    harness.fhir_override = reply
    with harness.client() as client:
        harness.login(client)
        assert harness.post(client, ALPHA + "/refresh").status_code == 202
        status = harness.wait(client)
        assert status.json()["attempt"]["error_code"] == code
        assert status.json()["serving"] == "unavailable"


@pytest.mark.parametrize(
    "status,times,code",
    [
        (401, 2, "unauthorized"),
        (403, 1, "forbidden"),
        (429, 2, "throttled"),
        (500, 1, "http_5xx"),
        (503, 2, "http_5xx"),
        (0, 2, "timeout"),
        (599, 2, "network"),
    ],
)
def test_response_retry_budgets(harness, status, times, code):
    harness.limits = replace(harness.limits, retry_delay_s=0.001, per_request_s=0.03)

    async def error(request):
        if status == 0:
            await asyncio.sleep(1)
        if status == 599:
            raise httpx.ConnectError(CONTENT)
        return httpx.Response(status, text=CONTENT)

    harness.fhir_override = error
    with harness.client() as client:
        harness.login(client)
        harness.post(client, ALPHA + "/refresh")
        result = harness.wait(client)
        assert result.json()["attempt"]["error_code"] == code
        assert CONTENT not in result.text
    assert sum(r.method == "GET" for r in harness.calls) == times
    assert sum(r.method == "POST" for r in harness.calls) == (2 if status == 401 else 1)


def test_renewal_is_once_for_entire_refresh(harness):
    failed_patient_once = False

    async def expire(request):
        nonlocal failed_patient_once
        if request.url.path.endswith(PATIENT_A) and not failed_patient_once:
            failed_patient_once = True
            return httpx.Response(401)
        return httpx.Response(401) if request.url.path.endswith("/Encounter") else None

    harness.fhir_override = expire
    with harness.client() as client:
        harness.login(client)
        harness.post(client, ALPHA + "/refresh")
        assert harness.wait(client).json()["attempt"]["error_code"] == "unauthorized"
    assert [r.method for r in harness.calls] == ["POST", "GET", "POST", "GET", "GET"]


@pytest.mark.parametrize("scope", ["patient", "session", "owner", "shutdown"])
def test_revocation_or_shutdown_while_active(harness, scope):
    async def slow(request):
        await asyncio.sleep(10)

    harness.fhir_override = slow
    with harness.client() as client:
        harness.login(client)
        response = harness.post(client, ALPHA + "/refresh")
        assert response.status_code == 202
        until = time.monotonic() + 1
        while len(harness.calls) < 2 and time.monotonic() < until:
            time.sleep(0.01)
        assert len(harness.calls) == 2
        if scope in {"patient", "session"}:
            path = ALPHA + "/revoke" if scope == "patient" else "/beta/auth/logout"
            assert harness.post(client, path).status_code == 204
            expected = 404 if scope == "patient" else 401
            assert client.get(ALPHA + "/status").status_code == expected
        if scope == "owner":
            client.portal.call(client.app.state.revoke_owner)
            assert client.get(ALPHA + "/status").status_code == 401
    assert len(harness.calls) == 2
    with harness.client() as client:
        if scope == "owner":
            assert harness.finish(client, harness.begin(client)).status_code == 401
        else:
            harness.login(client)
            if scope != "patient":
                result = client.get(ALPHA + "/status").json()["attempt"]
                assert result["state"] == "interrupted"
                assert result["error_code"] == ("shutdown" if scope == "shutdown" else "authorization_revoked")


def test_idempotency_concurrency_cooldown_and_restart(harness):
    hold = True

    async def wait_if_held(request):
        while hold:
            await asyncio.sleep(0.005)
        return None

    harness.fhir_override = wait_if_held
    with harness.client() as client:
        harness.login(client)
        first = harness.post(client, ALPHA + "/refresh")
        assert first.status_code == 202
        assert harness.post(client, ALPHA + "/refresh").json() == first.json()
        assert harness.post(client, BRAVO + "/refresh").status_code == 409
        assert harness.post(client, ALPHA + "/refresh", key="different-key-a").status_code == 409
        assert harness.post(client, BRAVO + "/refresh", key="different-key-b").status_code == 409
        hold = False
        assert harness.wait(client).json()["attempt"]["state"] == "succeeded"
        cool = harness.post(client, ALPHA + "/refresh", key="cooldown-test-key")
        assert (cool.status_code, cool.headers["Retry-After"]) == (429, "300")
        cookie = client.cookies.get("__Host-beta-session")
    with harness.client() as client:
        client.cookies.set("__Host-beta-session", cookie, path="/")
        assert client.get(ALPHA + "/status").status_code == 401
        client.cookies.clear()
        harness.login(client)
        assert harness.post(client, ALPHA + "/refresh").json() == first.json()
        assert harness.post(client, ALPHA + "/refresh", key="cooldown-test-key").status_code == 429
    assert len(harness.calls) == 9


@pytest.mark.parametrize("bucket", ["patient", "global", "outbound", "attempt"])
def test_quota_caps(harness, bucket):
    settings_by_bucket = {
        "patient": {"patient_per_hour": 1},
        "global": {"global_per_hour": 1},
        "outbound": {"global_requests_per_hour": 9},
        "attempt": {"requests_per_attempt": 2},
    }
    harness.limits = replace(harness.limits, cooldown_s=0, **settings_by_bucket[bucket])
    with harness.client() as client:
        harness.login(client)
        assert harness.post(client, ALPHA + "/refresh").status_code == 202
        result = harness.wait(client).json()["attempt"]
        if bucket == "attempt":
            assert (result["error_code"], len(harness.calls)) == ("request_limit", 2)
            return
        assert result["state"] == "succeeded"
        target = ALPHA if bucket == "patient" else BRAVO
        rejected = harness.post(client, target + "/refresh", key="different-quota-key")
        assert (rejected.status_code, rejected.headers["Retry-After"]) == (429, "3600")
    assert len(harness.calls) == 9


def test_absolute_deadline_caps_slow_stream(harness):
    harness.limits = replace(harness.limits, deadline_s=0.07, per_request_s=1)

    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(20):
                await asyncio.sleep(0.025)
                yield b" "

    async def slow(request):
        return httpx.Response(200, stream=SlowStream(), headers={"Content-Type": "application/json"})

    harness.fhir_override = slow
    with harness.client() as client:
        harness.login(client)
        harness.post(client, ALPHA + "/refresh")
        assert harness.wait(client).json()["attempt"]["error_code"] == "deadline"
    assert len(harness.calls) == 2


def test_stable_default_does_not_expose_beta_activation(runtime, monkeypatch):
    from api.main import app

    monkeypatch.setenv("CLINICAL_TRACE_BETA", "true")
    client = TestClient(app)
    assert client.get(ALPHA + "/status").status_code == 404
    assert client.get("/api/v2/system/status").status_code == 200


@pytest.mark.parametrize(
    "method,target",
    [
        ("POST", settings.FHIR_BASE + "/Encounter"),
        ("DELETE", settings.FHIR_BASE + "/Patient/" + PATIENT_A),
        ("GET", settings.FHIR_BASE + "/Patient/" + PATIENT_B),
        ("GET", settings.FHIR_BASE + "/Binary/invented-note"),
        ("GET", settings.FHIR_BASE + "/MedicationRequest?patient=" + PATIENT_A),
        ("GET", settings.FHIR_BASE + "/Observation?patient=" + PATIENT_A + "&category=unknown"),
        ("GET", settings.FHIR_BASE + "/Observation?patient=" + PATIENT_B + "&category=laboratory"),
        ("GET", settings.FHIR_BASE + "/Encounter?patient=" + PATIENT_A + "&_include=Encounter:subject"),
        ("GET", "https://evil.invalid/token"),
    ],
)
def test_transport_rejects_unapproved_operations_before_token(harness, keys, method, target):
    from src.interop.epic.beta_acquisition import Acquisition, AcquisitionError

    def forbidden():
        pytest.fail("an unapproved operation reached the acquisition budget")

    attempt = Acquisition(
        None,
        "invented-attempt",
        PATIENT_A,
        keys[1],
        httpx.MockTransport(harness.epic),
        harness.limits,
        time.monotonic() + 1,
        forbidden,
    )
    with pytest.raises(AcquisitionError, match="operation_scope"):
        asyncio.run(attempt.request(method, target))
    assert not harness.calls


def test_abandoned_attempt_recovers_without_replay_and_catalog_excludes_other_workers(harness):
    import sqlite3

    with harness.client() as client:
        harness.login(client)
        assert harness.post(client, ALPHA + "/refresh").status_code == 202
        result = harness.wait(client).json()["attempt"]
        assert result["state"] == "succeeded"
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            with harness.client():
                pytest.fail("second catalog opened while the first owned it")
    with sqlite3.connect(harness.db) as connection:
        connection.execute("UPDATE attempt SET state = 'running', finished_at = NULL, operations = NULL")
    with harness.client() as client:
        harness.login(client)
        recovered = client.get(ALPHA + "/status").json()["attempt"]
        assert recovered["attempt_id"] == result["attempt_id"]
        assert (recovered["state"], recovered["error_code"]) == ("interrupted", "restart")
        assert harness.post(client, ALPHA + "/refresh").json() == {"attempt_id": result["attempt_id"]}
        assert harness.post(client, ALPHA + "/refresh", key="recovery-quota-key").status_code == 429
    assert len(harness.calls) == 9


@pytest.mark.parametrize(
    "mode,code",
    [
        ("large", "response_too_large"),
        ("invalid", "malformed_token"),
        ("scope", "unapproved_grant"),
        ("duplicate", "malformed_token"),
        ("huge_expiry", "malformed_token"),
    ],
)
def test_bad_token_is_bounded_and_never_used(harness, mode, code):
    async def token(_):
        if mode == "large":
            return httpx.Response(200, content=b"X" * (64 * 1024 + 1))
        if mode == "invalid":
            return httpx.Response(200, json={"access_token": "not a bearer header"})
        if mode == "duplicate":
            return httpx.Response(
                200,
                content=(
                    b'{"access_token":"forged","access_token":"replacement","token_type":"Bearer","expires_in":300}'
                ),
            )
        payload = {"access_token": BEARER, "token_type": "Bearer", "expires_in": 300}
        if mode == "scope":
            payload["scope"] = "system/*.write"
        if mode == "huge_expiry":
            payload["expires_in"] = 10**400
        return httpx.Response(200, json=payload)

    harness.token_override = token
    with harness.client() as client:
        harness.login(client)
        harness.post(client, ALPHA + "/refresh")
        result = harness.wait(client).json()["attempt"]
        assert (result["state"], result["error_code"]) == ("failed", code)
    assert len(harness.calls) == 1


def test_retry_after_outside_absolute_budget_never_dispatches_a_retry(harness):
    harness.limits = replace(harness.limits, deadline_s=0.2)

    async def throttled(_):
        return httpx.Response(429, headers={"Retry-After": "90"}, text=CONTENT)

    harness.fhir_override = throttled
    with harness.client() as client:
        harness.login(client)
        harness.post(client, ALPHA + "/refresh")
        assert harness.wait(client).json()["attempt"]["error_code"] == "throttled"
    assert len(harness.calls) == 2


def test_unauthorized_renewal_does_not_reset_network_retry_budget(harness):
    harness.limits = replace(harness.limits, retry_delay_s=0)
    replies = iter((503, 401, 503, 503))

    async def alternating(_):
        return httpx.Response(next(replies))

    harness.fhir_override = alternating
    with harness.client() as client:
        harness.login(client)
        harness.post(client, ALPHA + "/refresh")
        assert harness.wait(client).json()["attempt"]["error_code"] == "http_5xx"
    assert [r.method for r in harness.calls] == ["POST", "GET", "GET", "POST", "GET"]


def test_retry_never_uses_expired_bearer(harness):
    async def short_lived(_):
        return httpx.Response(200, json={"access_token": BEARER, "token_type": "Bearer", "expires_in": 1})

    async def throttled(_):
        harness.clock += 2
        return httpx.Response(503)

    harness.limits = replace(harness.limits, retry_delay_s=0)
    harness.token_override = short_lived
    harness.fhir_override = throttled
    with harness.client() as client:
        harness.login(client)
        harness.post(client, ALPHA + "/refresh")
        assert harness.wait(client).json()["attempt"]["error_code"] == "token_expired"
    assert [r.method for r in harness.calls] == ["POST", "GET"]
