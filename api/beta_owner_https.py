"""Default-disabled trusted HTTPS bootstrap, diagnostic only. No enrollment, auth, Beta or LIVE routes.

No listener/browser is started here. No certificate is generated, installed or exempted from verification.
The supplied policy-host certificate must pass a normal system-trust/hostname TLS handshake before a
fixed-loopback single-process Uvicorn configuration is returned. Actual browser trust remains a separate
acceptance gate: a Python system-root check does not prove a particular browser's trust configuration.
"""

from __future__ import annotations

import ipaddress
import os
import ssl
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import uvicorn
from cryptography import x509
from cryptography.x509.oid import ExtendedKeyUsageOID

from api.beta_owner_custody import Custody
from api.beta_webauthn import LOCAL_BIND, LocalOwnerPolicy
from api.beta_webauthn_store import private_path


class BootstrapRejected(Exception):
    """Content-free failure; never echo certificate/key paths or library exceptions."""


def _system_trust() -> ssl.SSLContext:
    # Environment CA overrides must not silently substitute a custom trust root for system trust.
    if any(name in os.environ for name in ("SSL_CERT_FILE", "SSL_CERT_DIR", "SSLKEYLOGFILE")):
        raise BootstrapRejected
    client = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    client.hostname_checks_common_name = False  # require SAN, never legacy CN fallback
    client.minimum_version = ssl.TLSVersion.TLSv1_2
    return client


def _verify_handshake(server: ssl.SSLContext, client: ssl.SSLContext, *, hostname: str = "localhost") -> bytes:
    """Actual TLS proof in MemoryBIO: no sockets, DNS, certificate bypass or trust-store mutation."""
    if client.verify_mode != ssl.CERT_REQUIRED or not client.check_hostname:
        raise BootstrapRejected
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    peer_incoming, peer_outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    local = server.wrap_bio(incoming, outgoing, server_side=True)
    peer = client.wrap_bio(peer_incoming, peer_outgoing, server_hostname=hostname)
    done = [False, False]
    for _ in range(16):
        for index, endpoint in enumerate((local, peer)):
            try:
                endpoint.do_handshake()
                done[index] = True
            except ssl.SSLWantReadError:
                pass
        for source, target in ((outgoing, peer_incoming), (peer_outgoing, incoming)):
            data = source.read()
            if data:
                target.write(data)
        if all(done):
            return peer.getpeercert(binary_form=True) or b""
    raise BootstrapRejected


def _trusted_server(certificate: Path, key: Path, *, hostname: str = "localhost") -> ssl.SSLContext:
    # Hold both hierarchies and files through every read, OpenSSL reopen and actual handshake.
    try:
        with Custody(certificate.parent) as cert_custody, Custody(key.parent) as key_custody:
            cert_custody.file(certificate)
            key_custody.file(key)
            context = _load_trusted_server(certificate, key, hostname=hostname)
            cert_custody.check()
            key_custody.check()
            return context
    except Exception:
        raise BootstrapRejected from None


def _load_trusted_server(certificate: Path, key: Path, *, hostname: str = "localhost") -> ssl.SSLContext:
    try:
        for path in (certificate, key):
            private_path(path.parent, directory=True)
            private_path(path)
        leaf = x509.load_pem_x509_certificate(certificate.read_bytes())
        now = datetime.now(timezone.utc)
        names = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        if (
            not leaf.not_valid_before_utc <= now < leaf.not_valid_after_utc
            or list(names) != [x509.DNSName(hostname)]
            or leaf.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
            or ExtendedKeyUsageOID.SERVER_AUTH
            not in leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        ):
            raise BootstrapRejected
        server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server.minimum_version = ssl.TLSVersion.TLSv1_2
        # A callable prevents OpenSSL's interactive password prompt. Encrypted keys need a future reviewed unlock.
        server.load_cert_chain(certificate, key, password=lambda: "")
        peer_leaf = _verify_handshake(server, _system_trust(), hostname=hostname)
        from cryptography.hazmat.primitives.serialization import Encoding

        if peer_leaf != leaf.public_bytes(Encoding.DER):
            raise BootstrapRejected
        return server
    except Exception:
        raise BootstrapRejected from None


class _DiagnosticApp:
    def __init__(self, policy: LocalOwnerPolicy) -> None:
        self.policy = policy

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            return
        headers = scope.get("headers", [])
        hosts = [value for name, value in headers if name == b"host"]
        origins = [value for name, value in headers if name == b"origin"]
        try:
            loopback = ipaddress.ip_address(scope["client"][0]).is_loopback
        except (ValueError, TypeError, KeyError):
            loopback = False
        valid = (
            loopback
            and scope.get("scheme") == "https"
            and hosts == [self.policy.authority.encode()]
            and (not origins or origins == [self.policy.origin.encode()])
            and not scope.get("query_string")
            and not any(
                name.startswith((b"forwarded", b"x-forwarded", b"authorization", b"x-owner", b"cf-"))
                for name, _ in headers
            )
        )
        status, body = 403, b'{"error":"bootstrap_rejected"}'
        if valid:
            status, body = 404, b'{"error":"not_found"}'
            if (
                scope.get("method") == "GET"
                and scope.get("path") == "/owner/bootstrap"
                and scope.get("raw_path", b"/owner/bootstrap") == b"/owner/bootstrap"
                and not any(name in (b"content-length", b"transfer-encoding") for name, _ in headers)
            ):
                status = 200
                body = b'{"bootstrap":"review_required","enrollment":"disabled","live":"disabled"}'
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"cache-control", b"no-store"),
                    (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'"),
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"cross-origin-resource-policy", b"same-origin"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def prepare_bootstrap(
    policy: LocalOwnerPolicy, certificate: Path, key: Path, *, enabled: bool = False
) -> uvicorn.Config:
    """Prepare only the review diagnostic. No arbitrary app or genuine-auth enable path."""
    if enabled is not True:
        raise BootstrapRejected
    context = _trusted_server(certificate, key, hostname=policy.rp_id)
    config = uvicorn.Config(
        _DiagnosticApp(policy),
        host=LOCAL_BIND,
        port=urlsplit(policy.origin).port or 443,
        workers=1,
        reload=False,
        proxy_headers=False,
        forwarded_allow_ips="",
        access_log=False,
        log_config=None,
        log_level="warning",  # before config.load: inherited TRACE must not install request-scope middleware
        server_header=False,
        lifespan="off",
        ws="none",
        http="h11",
        interface="asgi3",
        limit_concurrency=8,
        timeout_keep_alive=5,
        timeout_graceful_shutdown=5,
        ssl_context_factory=lambda _config, _default: context,
    )
    config.load()  # reuse this exact validated in-memory TLS context; never reopen key files at bind time
    return config
