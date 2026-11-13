"""Synthetic software credentials only. Never imports an authenticator client, touches Windows Hello or key files.

Sign test responses with ephemeral test keys, then exercise the actual reviewed python-fido2 verifier.
"""

from __future__ import annotations

import ast
import hashlib
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from fido2.cose import ES256
from fido2.utils import websafe_decode, websafe_encode
from fido2.webauthn import (
    Aaguid,
    AttestationObject,
    AttestedCredentialData,
    AuthenticationResponse,
    AuthenticatorAssertionResponse,
    AuthenticatorAttestationResponse,
    AuthenticatorData,
    CollectedClientData,
    RegistrationResponse,
)

from api.beta_oidc import SESSION_ABSOLUTE, SESSION_COOKIE, SESSION_IDLE
from api.beta_webauthn import CEREMONY_S, ENROLLMENT_S, STEP_UP_S, LocalOwnerAuth, LocalOwnerPolicy
from api.beta_webauthn_http import create_local_owner_auth_app
from src.interop.epic.beta_catalog import RefreshRejected

ORIGIN = "https://localhost:8443"
HANDLE = b"synthetic-approved-owner-handle-0"
BROWSER = "b" * 43
HEADERS = {
    "Origin": ORIGIN,
    "X-Owner-Client": "webauthn-v1",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "same-origin",
    "Sec-Fetch-Dest": "empty",
    "Accept": "application/json",
}


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now


class SyntheticAuthenticator:
    """Test-only signer. Generates no usable Windows Hello credential and has no device/OS interaction."""

    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.id = b"invented-credential-id-0000000000"
        self.counter = 0

    def response(self, options, *, register=False, attack=None):
        challenge = websafe_decode(options["publicKey"]["challenge"])
        origin = ORIGIN
        if attack == "challenge":
            challenge = b"not-the-issued-challenge" * 2
        if attack == "origin":
            origin = "https://other.invalid"
        client = CollectedClientData.create(
            "webauthn.create" if register else "webauthn.get", challenge, origin, cross_origin=attack == "cross_origin"
        )
        if attack == "duplicate_client":
            raw = bytes(client).decode()
            client = CollectedClientData((raw[:-1] + ',"origin":"' + ORIGIN + '"}').encode())
        flags = AuthenticatorData.FLAG.UP | AuthenticatorData.FLAG.UV
        if attack == "uv":
            flags &= ~AuthenticatorData.FLAG.UV
        if attack == "up":
            flags &= ~AuthenticatorData.FLAG.UP
        if attack == "backup":
            flags |= AuthenticatorData.FLAG.BE
        if attack == "backed_up":
            flags |= AuthenticatorData.FLAG.BE | AuthenticatorData.FLAG.BS
        rp_hash = hashlib.sha256(b"other.invalid" if attack == "rp" else b"localhost").digest()
        raw_id = b"other-credential-id" if attack == "credential_id" else self.id
        if register:
            key = AttestedCredentialData.create(
                Aaguid.NONE, self.id, ES256.from_cryptography_key(self.key.public_key())
            )
            data = AuthenticatorData.create(rp_hash, flags | AuthenticatorData.FLAG.AT, self.counter, key)
            signature = self.key.sign(data + client.hash, ec.ECDSA(hashes.SHA256()))
            if attack == "signature":
                signature = signature[:-1] + bytes([signature[-1] ^ 1])
            attestation = AttestationObject.create("packed", data, {"alg": -7, "sig": signature})
            result = dict(
                RegistrationResponse(
                    raw_id=raw_id,
                    response=AuthenticatorAttestationResponse(client_data=client, attestation_object=attestation),
                )
            )
        else:
            if attack != "counter":
                self.counter += 1
            data = AuthenticatorData.create(rp_hash, flags, self.counter)
            signer = ec.generate_private_key(ec.SECP256R1()) if attack == "signature" else self.key
            signature = signer.sign(data + client.hash, ec.ECDSA(hashes.SHA256()))
            handle = b"other-owner" if attack == "handle" else HANDLE
            result = dict(
                AuthenticationResponse(
                    raw_id=raw_id,
                    response=AuthenticatorAssertionResponse(
                        client_data=client, authenticator_data=data, signature=signature, user_handle=handle
                    ),
                )
            )
        if attack == "id_mismatch":
            result["id"] = websafe_encode(b"id-does-not-match-rawId")
        return result


@pytest.fixture
def auth():
    clock = Clock()
    owner = LocalOwnerAuth(LocalOwnerPolicy(ORIGIN, HANDLE), clock=clock, authorize_enrollment=lambda: True)
    return owner, clock, SyntheticAuthenticator()


def enroll(owner, key):
    options = owner.begin_registration(owner.issue_enrollment_grant(), BROWSER)
    owner.complete_registration(options["flow_id"], BROWSER, key.response(options, register=True))


def login(owner, key):
    options = owner.begin_login(BROWSER)
    return owner.complete_login(options["flow_id"], BROWSER, key.response(options))


def rejected(call, code):
    with pytest.raises(RefreshRejected) as error:
        call()
    assert error.value.code == code


def test_controlled_registration_login_and_one_shot_step_up_use_the_real_verifier(auth):
    owner, _, key = auth
    rejected(lambda: owner.begin_login(BROWSER), "owner_not_enrolled")
    grant = owner.issue_enrollment_grant()
    options = owner.begin_registration(grant, BROWSER)
    assert options["publicKey"]["authenticatorSelection"]["userVerification"] == "required"
    assert options["publicKey"]["rp"]["id"] == "localhost"
    owner.complete_registration(options["flow_id"], BROWSER, key.response(options, register=True))
    rejected(owner.issue_enrollment_grant, "owner_already_enrolled")
    token, session = login(owner, key)
    assert owner.session(token) is session and session.auth.amr == ("webauthn", "uv")
    assert not owner.consume_elevation(token, session.csrf)  # login itself never elevates
    step = owner.begin_step_up(token, session.csrf, BROWSER)
    assert step["publicKey"]["userVerification"] == "required"
    owner.complete_step_up(step["flow_id"], BROWSER, key.response(step), token, session.csrf)
    assert owner.consume_elevation(token, session.csrf) is True
    assert owner.consume_elevation(token, session.csrf) is False
    owner.logout(token, session.csrf)
    assert owner.session(token) is None


def test_enrollment_is_denied_without_an_out_of_band_owner_authorizer():
    owner = LocalOwnerAuth(LocalOwnerPolicy(ORIGIN, HANDLE))
    rejected(owner.issue_enrollment_grant, "enrollment_not_authorized")
    rejected(lambda: owner.begin_registration("invented-grant", BROWSER), "enrollment_not_authorized")


def test_enrollment_grant_is_short_lived_and_single_use(auth):
    owner, clock, _ = auth
    grant = owner.issue_enrollment_grant()
    clock.now += ENROLLMENT_S
    rejected(lambda: owner.begin_registration(grant, BROWSER), "enrollment_not_authorized")
    grant = owner.issue_enrollment_grant()
    owner.begin_registration(grant, BROWSER)
    rejected(lambda: owner.begin_registration(grant, BROWSER), "enrollment_not_authorized")
    rejected(owner.issue_enrollment_grant, "enrollment_pending")


@pytest.mark.parametrize(
    "attack",
    [
        "challenge",
        "origin",
        "rp",
        "uv",
        "up",
        "backup",
        "backed_up",
        "cross_origin",
        "duplicate_client",
        "credential_id",
        "id_mismatch",
        "signature",
    ],
)
def test_registration_rejects_invalid_web_authn_evidence_and_spends_the_flow(auth, attack):
    owner, _, key = auth
    options = owner.begin_registration(owner.issue_enrollment_grant(), BROWSER)
    bad = key.response(options, register=True, attack=attack)
    rejected(lambda: owner.complete_registration(options["flow_id"], BROWSER, bad), "registration_rejected")
    rejected(
        lambda: owner.complete_registration(options["flow_id"], BROWSER, key.response(options, register=True)),
        "ceremony_rejected",
    )
    assert not owner.sessions


@pytest.mark.parametrize(
    "attack",
    [
        "challenge",
        "origin",
        "rp",
        "uv",
        "up",
        "backup",
        "backed_up",
        "cross_origin",
        "duplicate_client",
        "credential_id",
        "id_mismatch",
        "signature",
        "handle",
    ],
)
def test_login_requires_the_pinned_owner_key_and_signed_user_verification(auth, attack):
    owner, _, key = auth
    enroll(owner, key)
    options = owner.begin_login(BROWSER)
    bad = key.response(options, attack=attack)
    rejected(lambda: owner.complete_login(options["flow_id"], BROWSER, bad), "authentication_rejected")
    rejected(lambda: owner.complete_login(options["flow_id"], BROWSER, key.response(options)), "ceremony_rejected")
    assert not owner.sessions


@pytest.mark.parametrize("change", ["expired", "browser", "purpose"])
def test_ceremonies_expire_and_are_bound_to_browser_and_purpose(auth, change):
    owner, clock, key = auth
    options = owner.begin_registration(owner.issue_enrollment_grant(), BROWSER)
    payload = key.response(options, register=True)
    if change == "expired":
        clock.now += CEREMONY_S

    def call():
        if change == "purpose":
            return owner.complete_login(options["flow_id"], BROWSER, payload)
        return owner.complete_registration(options["flow_id"], "c" * 43 if change == "browser" else BROWSER, payload)

    rejected(call, "ceremony_rejected")
    assert not owner.sessions


def test_counter_rollback_is_rejected_and_new_login_revokes_old_sessions(auth):
    owner, _, key = auth
    enroll(owner, key)
    first, _ = login(owner, key)
    options = owner.begin_login(BROWSER)
    rejected(
        lambda: owner.complete_login(options["flow_id"], BROWSER, key.response(options, attack="counter")),
        "authentication_rejected",
    )
    second, _ = login(owner, key)
    assert owner.session(first) is None and owner.session(second) is not None


@pytest.mark.parametrize("kind", ["idle", "absolute"])
def test_local_sessions_expire_even_with_prior_valid_authentication(auth, kind):
    owner, clock, key = auth
    enroll(owner, key)
    token, session = login(owner, key)
    if kind == "idle":
        clock.now += SESSION_IDLE
        assert owner.session(token) is None
    else:
        deadline = session.created + SESSION_ABSOLUTE
        while clock.now < deadline:
            clock.now = min(clock.now + SESSION_IDLE - 1, deadline)
            current = owner.session(token, touch=True)
            assert (current is session) if clock.now < deadline else (current is None)


def test_zero_counter_authenticator_still_needs_fresh_single_use_signed_challenges(auth):
    owner, _, key = auth
    enroll(owner, key)
    options = owner.begin_login(BROWSER)
    payload = key.response(options, attack="counter")  # zero counters are valid; challenge consumption prevents replay
    first, _ = owner.complete_login(options["flow_id"], BROWSER, payload)
    rejected(lambda: owner.complete_login(options["flow_id"], BROWSER, payload), "ceremony_rejected")
    options = owner.begin_login(BROWSER)
    second, _ = owner.complete_login(options["flow_id"], BROWSER, key.response(options, attack="counter"))
    assert owner.session(first) is None and owner.session(second) is not None


@pytest.mark.parametrize("change", ["csrf", "session", "logout", "expiry", "revoke"])
def test_step_up_is_session_bound_expires_and_does_not_survive_revocation(auth, change):
    owner, clock, key = auth
    enroll(owner, key)
    token, session = login(owner, key)
    options = owner.begin_step_up(token, session.csrf, BROWSER)
    payload = key.response(options)
    if change == "csrf":
        rejected(lambda: owner.complete_step_up(options["flow_id"], BROWSER, payload, token, "x" * 43), "csrf_rejected")
    elif change in {"session", "logout", "revoke"}:
        if change == "session":
            login(owner, key)
        elif change == "logout":
            owner.logout(token, session.csrf)
        else:
            owner.revoke_owner()
        with pytest.raises(RefreshRejected):
            owner.complete_step_up(options["flow_id"], BROWSER, payload, token, session.csrf)
    else:
        owner.complete_step_up(options["flow_id"], BROWSER, payload, token, session.csrf)
        clock.now += STEP_UP_S
    assert owner.consume_elevation(token, session.csrf) is False


@pytest.mark.parametrize(
    "origin",
    [
        "http://localhost:8443",
        "https://other.invalid",
        "https://localhost:8443/",
        "https://localhost:8443?x=1",
        "https://u@localhost:8443",
        "https://localhost:0",
        "https://localhost:bad",
    ],
)
def test_only_one_explicit_local_https_origin_is_accepted(origin):
    with pytest.raises(ValueError):
        LocalOwnerPolicy(origin, HANDLE)


def test_origin_is_canonical_for_the_browsers_exact_origin_match():
    for origin in ("https://localhost:443", "HTTPS://localhost:8443", "https://localhost:8443#"):
        with pytest.raises(ValueError):
            LocalOwnerPolicy(origin, HANDLE)
    assert LocalOwnerPolicy("https://localhost", HANDLE).origin == "https://localhost"


def test_verified_step_up_supplies_the_existing_live_approval_callback_for_synthetic_evidence(
    auth, tmp_path, monkeypatch
):
    from cryptography.hazmat.primitives.asymmetric import rsa

    from src.interop.epic import settings
    from tests.test_beta_live_generations import World
    from tests.test_beta_live_route import LIVE_ALIAS, FakeEpic

    monkeypatch.setenv(settings.ENABLE_ENV, "1")  # only the existing in-process synthetic acquisition fixture
    world = World(tmp_path, rsa.generate_private_key(public_exponent=65537, key_size=2048), FakeEpic())
    try:
        owner, clock, key = auth
        clock.now = world.clock.now
        enroll(owner, key)
        token, session = login(owner, key)
        attempt, digest = world.candidate()

        def approve():
            return world.gens.approve(
                LIVE_ALIAS,
                attempt,
                digest,
                lambda: owner.session(token) is not None,
                lambda: owner.consume_elevation(token, session.csrf),
            )

        rejected(approve, "step_up_required")
        options = owner.begin_step_up(token, session.csrf, BROWSER)
        owner.complete_step_up(options["flow_id"], BROWSER, key.response(options), token, session.csrf)
        assert approve()["state"] == "approved"
        rejected(approve, "step_up_required")
        assert world.gens.real_promotion is False
    finally:
        world.catalog.close()


def post(client, path, body=None, **headers):
    return client.post("/owner/auth/" + path, json={} if body is None else body, headers=HEADERS | headers)


def test_auth_only_http_registration_login_session_csrf_step_up_logout_and_revoke(auth):
    owner, _, key = auth
    with pytest.raises(ValueError):
        create_local_owner_auth_app(owner)
    app = create_local_owner_auth_app(owner, enabled=True)
    with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 45000)) as client:
        assert post(client, "register/options").status_code == 403
        grant = owner.issue_enrollment_grant()
        options = post(client, "register/options", **{"X-Enrollment-Grant": grant}).json()
        assert post(
            client,
            "register/finish",
            {"flow_id": options["flow_id"], "credential": key.response(options, register=True)},
        ).json() == {"enrolled": True}
        assert client.get("/owner/auth/session", headers=HEADERS).status_code == 401
        options = post(client, "login/options").json()
        response = post(client, "login/finish", {"flow_id": options["flow_id"], "credential": key.response(options)})
        assert response.status_code == 200
        cookie = response.headers["set-cookie"]
        assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie and SESSION_COOKIE in cookie
        csrf = response.json()["csrf_token"]
        assert post(client, "logout").status_code == 403
        assert client.get("/owner/auth/session", headers=HEADERS).json()["method"] == "webauthn"
        for action in ("logout", "revoke"):
            if action == "revoke":
                options = post(client, "login/options").json()
                csrf = post(
                    client, "login/finish", {"flow_id": options["flow_id"], "credential": key.response(options)}
                ).json()["csrf_token"]
                assert post(client, "revoke", **{"X-CSRF-Token": csrf}).status_code == 401
                options = post(client, "step-up/options", **{"X-CSRF-Token": csrf}).json()
                assert (
                    post(
                        client,
                        "step-up/finish",
                        {"flow_id": options["flow_id"], "credential": key.response(options)},
                        **{"X-CSRF-Token": csrf},
                    ).status_code
                    == 200
                )
            assert post(client, action, **{"X-CSRF-Token": csrf}).status_code == 204
            assert client.get("/owner/auth/session", headers=HEADERS).status_code == 401
        assert post(client, "login/options").status_code == 401
        assert app.state.bind_host == "127.0.0.1"


@pytest.mark.parametrize("attack", ["remote", "http", "host", "origin", "profile", "forwarded", "query", "body"])
def test_http_boundary_rejects_remote_and_forged_requests_before_ceremony(auth, attack):
    owner, _, _ = auth
    app = create_local_owner_auth_app(owner, enabled=True)
    with TestClient(app, base_url=ORIGIN, client=("192.0.2.1" if attack == "remote" else "127.0.0.1", 45000)) as client:
        url, headers = "/owner/auth/login/options", dict(HEADERS)
        if attack == "http":
            url = "http://localhost:8443" + url
        elif attack == "host":
            headers["Host"] = "other.invalid"
        elif attack == "origin":
            headers["Origin"] = "https://other.invalid"
        elif attack == "profile":
            headers.pop("Sec-Fetch-Site")
        elif attack == "forwarded":
            headers["X-Forwarded-For"] = "127.0.0.1"
        elif attack == "query":
            url += "?enable=true"
        response = client.post(url, json={"unexpected": True} if attack == "body" else {}, headers=headers)
        assert response.status_code in {400, 403}
        assert response.headers["cache-control"] == "no-store" and "access-control-allow-origin" not in response.headers
        assert not owner.sessions and not owner._pending


def test_no_live_paths_grant_issuer_device_access_or_existing_app_wiring(auth, caplog):
    owner, _, key = auth
    with TestClient(
        create_local_owner_auth_app(owner, enabled=True), base_url=ORIGIN, client=("127.0.0.1", 45000)
    ) as client:
        for path in ("/beta/epic/patients", "/owner/auth/issue-enrollment-grant", "/owner/auth/enable-live"):
            assert client.post(path, json={}, headers=HEADERS).status_code == 404
    # Application loggers are captured at DEBUG. The third-party fido2 logger prints credential descriptors at
    # DEBUG, so it is held at WARNING here, as the owner HTTPS config enforces (log_level="warning"). This keeps
    # the result independent of logger state left by other tests.
    with caplog.at_level("DEBUG"), caplog.at_level("WARNING", logger="fido2"):
        enroll(owner, key)
        login(owner, key)
    assert key.id.hex() not in caplog.text and "fido2.client" not in sys.modules
    root = Path(__file__).resolve().parents[1]
    for name in (
        "api/main.py",
        "api/beta.py",
        "api/beta_local.py",
        "scripts/run_beta_local.py",
        "frontend/beta/main.tsx",
    ):
        source = (root / name).read_text(encoding="utf-8")
        assert "beta_webauthn" not in source and '"./webauthn"' not in source
    source = (root / "api/beta_webauthn.py").read_text(encoding="utf-8")
    imports = [node.module or "" for node in ast.walk(ast.parse(source)) if isinstance(node, ast.ImportFrom)]
    assert not any(name.startswith("cryptography") for name in imports)
    assert "register_complete(" in source and "authenticate_complete(" in source
