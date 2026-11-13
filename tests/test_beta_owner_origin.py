"""N1 dedicated-origin contract with invented identities, software signers and synthetic TLS only.

These checks do not establish browser trust, physical binding or hostname exclusivity. In particular,
the cookie model deliberately demonstrates SAME-HOST CROSS-PORT exposure; ISR-159-03 stays OPEN.
"""

from __future__ import annotations

import hashlib
import ssl
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from fastapi.testclient import TestClient
from fido2.cose import ES256
from fido2.utils import websafe_decode
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

from api import beta_owner_https as https
from api import beta_webauthn_store as storage
from api.beta_oidc import SESSION_COOKIE
from api.beta_webauthn import (
    OWNER_ORIGIN,
    OWNER_RP_ID,
    OWNER_SESSION_COOKIE,
    LocalOwnerAuth,
    LocalOwnerPolicy,
)
from api.beta_webauthn_http import FLOW_COOKIE, PROFILE, create_local_owner_auth_app
from api.beta_webauthn_store import PURPOSE
from src.interop.epic.beta_catalog import RefreshRejected
from tests.test_beta_owner_https import response as diagnostic_response
from tests.test_beta_webauthn import BROWSER, HANDLE, Clock

AUTH_HEADERS = dict(PROFILE) | {"origin": OWNER_ORIGIN}
AUTHORITY = "owner.clinical-trace.localhost:8443"
INVENTED_SID = "S-1-5-21-111111111-222222222-333333333-1001"


class DedicatedSigner:
    """Ephemeral software proof, never a device/Windows Hello credential or attestation trust anchor."""

    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.identifier = b"synthetic-dedicated-origin-credential"
        self.counter = 0

    def proof(self, options, *, register=False, origin=OWNER_ORIGIN, rp=OWNER_RP_ID, uv=True):
        client = CollectedClientData.create(
            "webauthn.create" if register else "webauthn.get",
            websafe_decode(options["publicKey"]["challenge"]),
            origin,
            cross_origin=False,
        )
        flags = AuthenticatorData.FLAG.UP | (AuthenticatorData.FLAG.UV if uv else 0)
        if register:
            credential = AttestedCredentialData.create(
                Aaguid.NONE, self.identifier, ES256.from_cryptography_key(self.key.public_key())
            )
            data = AuthenticatorData.create(
                hashlib.sha256(rp.encode()).digest(), flags | AuthenticatorData.FLAG.AT, 0, credential
            )
            signature = self.key.sign(data + client.hash, ec.ECDSA(hashes.SHA256()))
            return dict(
                RegistrationResponse(
                    raw_id=self.identifier,
                    response=AuthenticatorAttestationResponse(
                        client_data=client,
                        attestation_object=AttestationObject.create("packed", data, {"alg": -7, "sig": signature}),
                    ),
                )
            )
        self.counter += 1
        data = AuthenticatorData.create(hashlib.sha256(rp.encode()).digest(), flags, self.counter)
        return dict(
            AuthenticationResponse(
                raw_id=self.identifier,
                response=AuthenticatorAssertionResponse(
                    client_data=client,
                    authenticator_data=data,
                    signature=self.key.sign(data + client.hash, ec.ECDSA(hashes.SHA256())),
                    user_handle=HANDLE,
                ),
            )
        )


def dedicated_owner(*, authorize=False):
    return LocalOwnerAuth(
        LocalOwnerPolicy(OWNER_ORIGIN, HANDLE),
        clock=Clock(),
        authorize_enrollment=(lambda: True) if authorize else None,
    )


def test_dedicated_origin_is_explicit_and_separate_from_legacy_synthetic_localhost():
    policy = LocalOwnerPolicy(OWNER_ORIGIN, HANDLE)
    assert (policy.origin, policy.rp_id, policy.authority, policy.session_cookie, policy.purpose) == (
        "https://owner.clinical-trace.localhost:8443",
        "owner.clinical-trace.localhost",
        AUTHORITY,
        "__Host-ct-owner-session",
        PURPOSE,
    )
    legacy = LocalOwnerPolicy("https://localhost:8443", HANDLE)
    assert legacy.rp_id == "localhost" and legacy.session_cookie == SESSION_COOKIE
    assert legacy != policy
    with pytest.raises(ValueError):
        LocalOwnerPolicy(OWNER_ORIGIN, HANDLE, purpose="genuine")
    owner = dedicated_owner()
    with pytest.raises(RefreshRejected, match="enrollment_not_authorized"):
        owner.issue_enrollment_grant()
    with pytest.raises(ValueError):
        create_local_owner_auth_app(owner)


@pytest.mark.parametrize(
    "origin",
    [
        "http://owner.clinical-trace.localhost:8443",
        "https://owner.clinical-trace.localhost",
        "https://owner.clinical-trace.localhost:443",
        "https://owner.clinical-trace.localhost:9443",
        "https://OWNER.clinical-trace.localhost:8443",
        "https://owner.clinical-trace.localhost.:8443",
        "https://sub.owner.clinical-trace.localhost:8443",
        "https://other.clinical-trace.localhost:8443",
        "https://user@owner.clinical-trace.localhost:8443",
        OWNER_ORIGIN + "/",
        OWNER_ORIGIN + "?enable=1",
        OWNER_ORIGIN + "#",
    ],
)
def test_dedicated_policy_rejects_any_unapproved_origin_variant(origin):
    with pytest.raises(ValueError):
        LocalOwnerPolicy(origin, HANDLE)


@pytest.mark.parametrize("change", [{"origin": "https://localhost:8443"}, {"rp": "localhost"}, {"uv": False}])
def test_dedicated_registration_uses_real_verifier_and_rejects_legacy_binding_or_missing_uv(change):
    owner, signer = dedicated_owner(authorize=True), DedicatedSigner()
    options = owner.begin_registration(owner.issue_enrollment_grant(), BROWSER)
    assert options["publicKey"]["rp"]["id"] == OWNER_RP_ID
    with pytest.raises(RefreshRejected, match="registration_rejected"):
        owner.complete_registration(options["flow_id"], BROWSER, signer.proof(options, register=True, **change))
    with pytest.raises(RefreshRejected, match="ceremony_rejected"):
        owner.complete_registration(options["flow_id"], BROWSER, signer.proof(options, register=True))
    assert not owner.sessions


def post(client, action, body=None, *, headers=None):
    return client.post(
        "/owner/auth/" + action, json={} if body is None else body, headers=AUTH_HEADERS | (headers or {})
    )


def test_dedicated_http_proof_cookie_csrf_logout_and_cookie_model_are_explicitly_not_port_isolation():
    owner, signer = dedicated_owner(authorize=True), DedicatedSigner()
    with TestClient(
        create_local_owner_auth_app(owner, enabled=True), base_url=OWNER_ORIGIN, client=("127.0.0.1", 45000)
    ) as client:
        grant = owner.issue_enrollment_grant()
        options = post(client, "register/options", headers={"x-enrollment-grant": grant}).json()
        assert post(
            client,
            "register/finish",
            {"flow_id": options["flow_id"], "credential": signer.proof(options, register=True)},
        ).json() == {"enrolled": True}
        options = post(client, "login/options").json()
        result = post(client, "login/finish", {"flow_id": options["flow_id"], "credential": signer.proof(options)})
        assert result.status_code == 200
        session_cookie = next(
            value for value in result.headers.get_list("set-cookie") if value.startswith(OWNER_SESSION_COOKIE + "=")
        )
        for attribute in ("Secure", "HttpOnly", "SameSite=strict", "Path=/"):
            assert attribute in session_cookie
        assert "domain=" not in session_cookie.lower() and SESSION_COOKIE + "=" not in session_cookie
        assert client.get("/owner/auth/session", headers=AUTH_HEADERS).status_code == 200
        csrf = result.json()["csrf_token"]
        token = client.cookies.get(OWNER_SESSION_COOKIE)
        client.cookies.clear()
        assert (
            client.get(
                "/owner/auth/session", headers=AUTH_HEADERS | {"cookie": SESSION_COOKIE + "=" + token}
            ).status_code
            == 401
        )
        duplicate = f"{OWNER_SESSION_COOKIE}={token}; {OWNER_SESSION_COOKIE}={token}"
        assert (
            client.get("/owner/auth/session", headers=AUTH_HEADERS | {"cookie": duplicate}).json()["error"]["code"]
            == "invalid_cookie"
        )
        client.cookies.set(OWNER_SESSION_COOKIE, token, domain=OWNER_RP_ID, path="/")
        assert post(client, "logout", headers={"x-csrf-token": "x" * 43}).status_code == 403
        result = post(client, "logout", headers={"x-csrf-token": csrf})
        assert result.status_code == 204
        deleted = result.headers.get_list("set-cookie")
        assert any(value.startswith(OWNER_SESSION_COOKIE + "=") and "Max-Age=0" in value for value in deleted)
        assert any(value.startswith(FLOW_COOKIE + "=") and "Max-Age=0" in value for value in deleted)
        assert owner.session(token) is None

    # httpx's cookie jar is an offline MODEL, not ordinary-browser acceptance or a SameSite/Host-prefix oracle.
    jar = httpx.Cookies()
    jar.extract_cookies(
        httpx.Response(200, headers={"set-cookie": session_cookie}, request=httpx.Request("GET", OWNER_ORIGIN))
    )
    for origin, expected in [
        (OWNER_ORIGIN, True),
        ("https://owner.clinical-trace.localhost:9443", True),  # cookies have NO port boundary
        ("https://localhost:8443", False),
        ("https://beta.invalid:8443", False),
        ("http://owner.clinical-trace.localhost:8443", False),
    ]:
        request = httpx.Request("GET", origin + "/owner/auth/session")
        jar.set_cookie_header(request)
        assert (OWNER_SESSION_COOKIE + "=" in request.headers.get("cookie", "")) is expected


@pytest.mark.parametrize(
    "attack",
    [
        "wrong_host",
        "wrong_port",
        "wrong_origin",
        "legacy_origin",
        "http",
        "duplicate_host",
        "duplicate_origin",
        "forwarded",
        "remote",
    ],
)
def test_dedicated_http_boundary_rejects_request_variants_before_any_ceremony(attack):
    owner = dedicated_owner()
    headers = list(AUTH_HEADERS.items())
    url = OWNER_ORIGIN + "/owner/auth/register/options"
    if attack == "wrong_host":
        headers.append(("host", "other.clinical-trace.localhost:8443"))
    elif attack == "wrong_port":
        url = "https://owner.clinical-trace.localhost:9443/owner/auth/register/options"
    elif attack in {"wrong_origin", "legacy_origin"}:
        headers = [(name, value) for name, value in headers if name != "origin"]
        headers.append(
            (
                "origin",
                "https://owner.clinical-trace.localhost:9443" if attack == "wrong_origin" else "https://localhost:8443",
            )
        )
    elif attack == "http":
        url = url.replace("https:", "http:")
    elif attack == "duplicate_host":
        headers.extend([("host", AUTHORITY), ("host", AUTHORITY)])
    elif attack == "duplicate_origin":
        headers.append(("origin", OWNER_ORIGIN))
    elif attack == "forwarded":
        headers.append(("forwarded", "host=" + AUTHORITY))
    with TestClient(
        create_local_owner_auth_app(owner, enabled=True),
        base_url=OWNER_ORIGIN,
        client=("192.0.2.1" if attack == "remote" else "127.0.0.1", 45000),
    ) as client:
        result = client.post(url, headers=headers, json={})
    assert result.status_code in {400, 403}
    expected = (
        "invalid_origin"
        if attack in {"wrong_origin", "legacy_origin", "duplicate_origin"}
        else ("untrusted_header" if attack == "forwarded" else "local_https_required")
    )
    assert result.json()["error"]["code"] == expected  # do not let default-deny enrollment mask a boundary regression
    assert not owner._pending and not owner._grants


def test_dedicated_diagnostic_uses_exact_authority_without_issuing_auth_or_live_capabilities():
    app = https._DiagnosticApp(LocalOwnerPolicy(OWNER_ORIGIN, HANDLE))
    start, body = diagnostic_response(app, headers=[(b"host", AUTHORITY.encode()), (b"origin", OWNER_ORIGIN.encode())])
    assert start["status"] == 200
    assert b'"enrollment":"disabled"' in body and b'"live":"disabled"' in body
    assert b"set-cookie" not in dict(start["headers"])
    start, _ = diagnostic_response(app, headers=[(b"host", b"owner.clinical-trace.localhost:9443")])
    assert start["status"] == 403


@pytest.fixture
def dedicated_certificates(tmp_path, monkeypatch):
    """An isolated invented CA; neither system roots nor actual principal APIs are read."""
    ca_key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "N1 synthetic offline CA")])
    now = datetime.now(timezone.utc)
    ca = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(hours=1))
        .not_valid_after(now + timedelta(hours=2))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    client = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client.load_verify_locations(cadata=ca.public_bytes(serialization.Encoding.PEM).decode())
    client.hostname_checks_common_name = False
    monkeypatch.setattr(https, "_system_trust", lambda: client)
    # Custody is not replaced in production; this NEW test isolates SAN/handshake propagation without OS identity APIs.
    monkeypatch.setattr(https, "private_path", lambda *args, **kwargs: (1, 1))
    index = 0

    def make(names):
        nonlocal index
        index += 1
        key = ec.generate_private_key(ec.SECP256R1())
        leaf = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(hours=1))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(value) for value in names]), critical=False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(ca_key, hashes.SHA256())
        )
        certificate, private_key = tmp_path / f"invented-leaf-{index}.pem", tmp_path / f"invented-key-{index}.key"
        certificate.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
        private_key.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
            )
        )
        return certificate, private_key, client

    return make


def test_dedicated_tls_binds_exact_san_sni_and_actual_peer_leaf_with_synthetic_roots(
    dedicated_certificates, monkeypatch
):
    certificate, key, client = dedicated_certificates([OWNER_RP_ID])
    held = []

    class SyntheticCustody:
        def __init__(self, root):
            self.root = root

        def __enter__(self):
            return self

        def file(self, path):
            held.append(path)

        def check(self):
            held.append(self.root)

        def __exit__(self, *args):
            return None

    monkeypatch.setattr(https, "Custody", SyntheticCustody)
    config = https.prepare_bootstrap(LocalOwnerPolicy(OWNER_ORIGIN, HANDLE), certificate, key, enabled=True)
    context = config.ssl
    assert certificate in held and key in held and held.count(certificate.parent) == 2
    assert config.host == "127.0.0.1" and config.port == 8443 and isinstance(config.app, https._DiagnosticApp)
    assert https._verify_handshake(context, client, hostname=OWNER_RP_ID)
    with pytest.raises(ssl.SSLCertVerificationError):
        https._verify_handshake(context, client, hostname="localhost")
    actual = https._verify_handshake

    def different_peer(server, trust, *, hostname="localhost"):
        actual(server, trust, hostname=hostname)
        return b"different-synthetic-leaf"

    monkeypatch.setattr(https, "_verify_handshake", different_peer)
    with pytest.raises(https.BootstrapRejected):
        https._load_trusted_server(certificate, key, hostname=OWNER_RP_ID)


@pytest.mark.parametrize("names", [["localhost"], ["*.clinical-trace.localhost"], [OWNER_RP_ID, "localhost"]])
def test_dedicated_tls_refuses_legacy_wildcard_and_multi_san_certificates(dedicated_certificates, names):
    certificate, key, _ = dedicated_certificates(names)
    with pytest.raises(https.BootstrapRejected):
        https._load_trusted_server(certificate, key, hostname=OWNER_RP_ID)


def test_dedicated_bootstrap_passes_pinned_hostname_and_stays_diagnostic_only(tmp_path, monkeypatch):
    seen = []
    sentinel = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)

    def trusted(certificate, key, *, hostname="localhost"):
        seen.append(hostname)
        return sentinel

    monkeypatch.setattr(https, "_trusted_server", trusted)
    policy = LocalOwnerPolicy(OWNER_ORIGIN, HANDLE)
    with pytest.raises(https.BootstrapRejected):
        https.prepare_bootstrap(policy, tmp_path / "not-read.pem", tmp_path / "not-read.key")
    assert not seen
    config = https.prepare_bootstrap(policy, tmp_path / "not-read.pem", tmp_path / "not-read.key", enabled=True)
    assert seen == [OWNER_RP_ID]
    assert config.ssl is sentinel and config.host == "127.0.0.1" and config.port == 8443
    assert isinstance(config.app, https._DiagnosticApp)


@pytest.fixture
def invented_sid_store(monkeypatch):
    """Binding checks only: fake OS identity/custody, actual synthetic-file identities and SQLite.

    This does not verify Windows ACLs, console authorization, physical custody or genuine provisioning.
    The fake creates only explicitly requested synthetic fixture files and never initializes on reopen.
    """
    acquired = []

    class SyntheticCustody:
        def __init__(self, root):
            root.stat()
            acquired.append(root)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

        def file(self, path, *, create=False, writable=False):
            if create:
                with path.open("xb"):
                    pass
            path.stat()

        def check(self):
            return None

        def close(self):
            return None

    def fixture_identity(path, *, directory=False):
        info = path.stat()
        return info.st_dev, info.st_ino

    monkeypatch.setattr(storage, "current_principal", lambda: INVENTED_SID)
    monkeypatch.setattr(storage, "Custody", SyntheticCustody)
    monkeypatch.setattr(storage, "private_path", fixture_identity)
    return acquired


def test_invented_sid_unknown_principal_and_missing_dedicated_state_deny_without_initializing(
    tmp_path, invented_sid_store
):
    policy = LocalOwnerPolicy(OWNER_ORIGIN, HANDLE)
    unapproved = tmp_path / "unapproved-owner"
    with pytest.raises(storage.StoreUnavailable):
        storage.OwnerCredentialStore.provision(
            unapproved,
            policy,
            "S-1-5-21-111111111-222222222-333333333-1002",
            authorize=lambda: True,
            synthetic_only=True,
        )
    assert not unapproved.exists() and not invented_sid_store

    missing = tmp_path / "missing-owner-state"
    missing.mkdir()
    with pytest.raises(storage.StoreUnavailable):
        storage.OwnerCredentialStore(missing, policy, INVENTED_SID)
    assert list(missing.iterdir()) == []  # opening is not provision/reset/recovery


def test_invented_sid_dedicated_store_binds_origin_handle_purpose_without_legacy_migration(
    tmp_path, invented_sid_store
):
    policy = LocalOwnerPolicy(OWNER_ORIGIN, HANDLE)
    store = storage.OwnerCredentialStore.provision(
        tmp_path / "dedicated-synthetic-owner",
        policy,
        INVENTED_SID,
        authorize=lambda: True,
        synthetic_only=True,
    )
    try:
        assert store.load().credential is None
        assert store._connection.execute("SELECT origin,handle,principal,purpose FROM owner_state").fetchall() == [
            (OWNER_ORIGIN, HANDLE, INVENTED_SID, PURPOSE)
        ]
    finally:
        store.close()
    dedicated_bytes = store.path.read_bytes()
    with pytest.raises(storage.StoreUnavailable):
        storage.OwnerCredentialStore(
            store.root, LocalOwnerPolicy(OWNER_ORIGIN, b"other-invented-owner-handle-00000"), INVENTED_SID
        )
    assert store.path.read_bytes() == dedicated_bytes

    # A legacy localhost test store remains legacy. Denial must not rewrite its immutable bindings.
    legacy_policy = LocalOwnerPolicy("https://localhost:8443", HANDLE)
    legacy = storage.OwnerCredentialStore.provision(
        tmp_path / "legacy-synthetic-owner",
        legacy_policy,
        INVENTED_SID,
        authorize=lambda: True,
        synthetic_only=True,
    )
    legacy.close()
    before = legacy.path.read_bytes()
    with pytest.raises(storage.StoreUnavailable):
        storage.OwnerCredentialStore(legacy.root, policy, INVENTED_SID)
    assert legacy.path.read_bytes() == before
    reopened = storage.OwnerCredentialStore(legacy.root, legacy_policy, INVENTED_SID)
    try:
        assert reopened._connection.execute("SELECT origin,handle,principal,purpose FROM owner_state").fetchall() == [
            (legacy_policy.origin, HANDLE, INVENTED_SID, PURPOSE)
        ]
        assert reopened.load().credential is None
    finally:
        reopened.close()
