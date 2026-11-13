"""Synthetic TLS chains in isolated files/MemoryBIO only. Never install or inspect real certificates."""

from __future__ import annotations

import os
import ssl
from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from api import beta_owner_https as https
from api.beta_webauthn import LocalOwnerPolicy
from tests.test_beta_webauthn import HANDLE, ORIGIN


@pytest.fixture
def certificates(tmp_path, monkeypatch):
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Synthetic offline test root")])
    now = datetime.now(timezone.utc)
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    client = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client.load_verify_locations(cadata=ca.public_bytes(serialization.Encoding.PEM).decode())
    client.hostname_checks_common_name = False
    monkeypatch.setattr(https, "_system_trust", lambda: client)

    def make(kind="valid"):
        key = ec.generate_private_key(ec.SECP256R1())
        name = "other.invalid" if kind == "hostname" else "localhost"
        builder = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
            .issuer_name(ca_name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now + timedelta(hours=1) if kind == "future" else now - timedelta(hours=1))
            .not_valid_after(now - timedelta(minutes=1) if kind == "expired" else now + timedelta(hours=2))
            .add_extension(x509.BasicConstraints(ca=kind == "ca_leaf", path_length=None), critical=True)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        )
        if kind != "cn_only":
            builder = builder.add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
        if kind != "no_eku":
            eku = ExtendedKeyUsageOID.CLIENT_AUTH if kind == "client_eku" else ExtendedKeyUsageOID.SERVER_AUTH
            builder = builder.add_extension(x509.ExtendedKeyUsage([eku]), critical=False)
        leaf = builder.sign(ca_key, hashes.SHA256())
        certificate, private_key = tmp_path / (kind + ".pem"), tmp_path / (kind + ".key")
        certificate.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
        private_key.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
            )
        )
        os.chmod(certificate, 0o600)
        os.chmod(private_key, 0o600)
        return certificate, private_key, client

    return make


def test_bootstrap_is_disabled_before_any_certificate_or_key_access(tmp_path, monkeypatch):
    monkeypatch.setattr(https, "_trusted_server", lambda *_: pytest.fail("disabled bootstrap touched key files"))
    with pytest.raises(https.BootstrapRejected):
        https.prepare_bootstrap(LocalOwnerPolicy(ORIGIN, HANDLE), tmp_path / "absent.pem", tmp_path / "absent.key")


def test_verified_tls_context_is_reused_with_fixed_loopback_and_closed_auth_live_surface(certificates):
    certificate, key, client = certificates()
    config = https.prepare_bootstrap(LocalOwnerPolicy(ORIGIN, HANDLE), certificate, key, enabled=True)
    assert config.host == "127.0.0.1" and config.port == 8443
    assert config.workers == 1 and not config.reload and not config.proxy_headers
    assert config.forwarded_allow_ips == "" and not config.access_log and not config.server_header
    assert config.ssl.minimum_version >= ssl.TLSVersion.TLSv1_2
    certificate.unlink()
    key.unlink()
    https._verify_handshake(config.ssl, client)  # config uses the validated context, without reopening files
    assert config.ssl_keyfile is None and config.ssl_certfile is None


@pytest.mark.parametrize("kind", ["hostname", "expired", "future", "ca_leaf", "cn_only", "no_eku", "client_eku"])
def test_unsafe_certificate_is_refused_without_listening(certificates, kind):
    certificate, key, _ = certificates(kind)
    with pytest.raises(https.BootstrapRejected):
        https.prepare_bootstrap(LocalOwnerPolicy(ORIGIN, HANDLE), certificate, key, enabled=True)


def test_untrusted_certificate_and_mismatched_private_key_are_refused(certificates, monkeypatch):
    certificate, key, _ = certificates()
    monkeypatch.setattr(https, "_system_trust", lambda: ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT))
    with pytest.raises(https.BootstrapRejected):
        https.prepare_bootstrap(LocalOwnerPolicy(ORIGIN, HANDLE), certificate, key, enabled=True)
    _, other_key, _ = certificates("another")
    with pytest.raises(https.BootstrapRejected):
        https.prepare_bootstrap(LocalOwnerPolicy(ORIGIN, HANDLE), certificate, other_key, enabled=True)


def test_insecure_client_context_is_never_accepted_as_proof_of_trust(certificates, monkeypatch):
    certificate, key, _ = certificates()
    insecure = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    insecure.check_hostname = False
    insecure.verify_mode = ssl.CERT_NONE
    monkeypatch.setattr(https, "_system_trust", lambda: insecure)
    with pytest.raises(https.BootstrapRejected):
        https.prepare_bootstrap(LocalOwnerPolicy(ORIGIN, HANDLE), certificate, key, enabled=True)


@pytest.mark.parametrize("variable", ["SSL_CERT_FILE", "SSL_CERT_DIR", "SSLKEYLOGFILE"])
def test_ca_environment_override_is_refused_before_loading_system_trust(monkeypatch, variable):
    monkeypatch.setenv(variable, "synthetic-unapproved-ca")
    with pytest.raises(https.BootstrapRejected):
        https._system_trust()


def response(app, **changes):
    scope = {
        "type": "http",
        "scheme": "https",
        "client": ("127.0.0.1", 45000),
        "headers": [(b"host", b"localhost:8443")],
        "method": "GET",
        "path": "/owner/bootstrap",
        "query_string": b"",
    } | changes
    events = []

    async def send(event):
        events.append(event)

    async def receive():
        pytest.fail("diagnostic must never consume credential/request bodies")

    coroutine = app(scope, receive, send)
    # The tiny ASGI diagnostic has only immediate in-memory sends; no event loop or IPC socket is needed.
    with pytest.raises(StopIteration):
        coroutine.send(None)
    return events[0], events[1]["body"]


def test_bootstrap_reports_review_required_and_never_issues_cookie_or_enrollment_capability():
    start, body = response(https._DiagnosticApp(LocalOwnerPolicy(ORIGIN, HANDLE)))
    assert start["status"] == 200
    assert body == b'{"bootstrap":"review_required","enrollment":"disabled","live":"disabled"}'
    headers = dict(start["headers"])
    assert headers[b"cache-control"] == b"no-store" and b"set-cookie" not in headers
    assert b"access-control-allow-origin" not in headers


@pytest.mark.parametrize(
    "changes",
    [
        {"client": ("192.0.2.1", 45000)},
        {"scheme": "http"},
        {"headers": [(b"host", b"other.invalid")]},
        {"headers": [(b"host", b"localhost:8443"), (b"host", b"localhost:8443")]},
        {"headers": [(b"host", b"localhost:8443"), (b"origin", b"https://other.invalid")]},
        {"headers": [(b"host", b"localhost:8443"), (b"x-forwarded-for", b"127.0.0.1")]},
        {"query_string": b"enable=true"},
        {"method": "POST"},
        {"raw_path": b"/owner/%62ootstrap"},
        {"headers": [(b"host", b"localhost:8443"), (b"content-length", b"1")]},
        {"path": "/owner/auth/register/options"},
        {"path": "/owner/auth/login/options"},
        {"path": "/beta/app/"},
        {"path": "/beta/epic/patients"},
    ],
)
def test_diagnostic_has_no_auth_live_or_request_boundary_bypass(changes):
    start, body = response(https._DiagnosticApp(LocalOwnerPolicy(ORIGIN, HANDLE)), **changes)
    assert start["status"] in {403, 404}
    assert b"credential" not in body and b"set-cookie" not in dict(start["headers"])
