"""Cloudflare Access application-token verification for the hosted Beta backend.

The hosted Beta Worker sits behind Cloudflare Access, which forwards every request with its signed application token in
``Cf-Access-Jwt-Assertion``. The Worker checks it before the backend container starts; this module checks it again
inside the backend, so a request that skips the Worker still cannot reach Epic.

The rules are fixed: RS256 only, a key from the team's own certificate endpoint, the exact team issuer, the
application's audience tag, a current ``exp``/``nbf``/``iat``, an application (not service) token, and an email equal
to the one configured owner. No claim is ever logged or returned.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from api.beta_oidc_keys import KID_SHAPE, ProviderError, parse_jwks
from src.interop.epic.beta_acquisition import AcquisitionError, bounded_response, object_json

TEAM_DOMAIN = re.compile(r"https://[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.cloudflareaccess\.com")
AUDIENCE = re.compile(r"[a-f0-9]{64}")
EMAIL = re.compile(r"[!-?A-~]{1,64}@[A-Za-z0-9.-]{1,253}")
ASCII_CLAIM = re.compile(r"[!-~]{3,320}")
TOKEN = re.compile(r"[A-Za-z0-9_-]{2,4096}\.[A-Za-z0-9_-]{2,16384}\.[A-Za-z0-9_-]{2,1024}")
HEADER = "cf-access-jwt-assertion"
CERTS_PATH = "/cdn-cgi/access/certs"
CERTS_LIMIT = 64 * 1024
KEYS_FRESH_S = 600  # refetch the team keys at least this often, so a rotated-out key stops working soon
KEYS_MIN_REFRESH_S = 30  # at most one fetch per window, so unknown key ids cannot drive traffic
KEYS_STALE_MAX_S = 6 * 3600  # an unreachable endpoint may keep a cached key usable for at most this long
SKEW_S = 60


class AccessRejected(Exception):
    """A fixed, content-free reason; the HTTP layer maps every one to 401 ``owner_required``."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class AccessPolicy:
    team_domain: str
    audience: str
    owner_email: str = field(repr=False)

    def __post_init__(self) -> None:
        if not TEAM_DOMAIN.fullmatch(self.team_domain or ""):
            raise ValueError("Access team domain must be https://<team>.cloudflareaccess.com")
        if not AUDIENCE.fullmatch(self.audience or ""):
            raise ValueError("Access audience must be the application's 64-hex AUD tag")
        if not EMAIL.fullmatch(self.owner_email or "") or self.owner_email != self.owner_email.strip():
            raise ValueError("owner email is malformed")


@dataclass(frozen=True)
class AccessIdentity:
    email: str
    expires: float
    issued: float


def _segment(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def access_transport() -> httpx.AsyncHTTPTransport:
    """TLS-verifying, no ambient proxy or CA overrides, no retries."""
    return httpx.AsyncHTTPTransport(verify=True, trust_env=False, retries=0)


class AccessVerifier:
    def __init__(
        self,
        policy: AccessPolicy,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.policy, self.clock = policy, clock
        self.transport = transport if transport is not None else access_transport()
        self.certs_url = policy.team_domain + CERTS_PATH
        self.keys: dict[str, rsa.RSAPublicKey] = {}
        self.fetched: float | None = None
        self.attempted: float | None = None
        self._lock = asyncio.Lock()

    async def _refresh(self) -> None:
        request = httpx.Request("GET", self.certs_url, headers={"Accept": "application/json", "Accept-Encoding": "identity"})
        try:
            status, _, raw = await bounded_response(self.transport, request, seconds=5, limit=CERTS_LIMIT)
            if status != 200:
                raise AccessRejected("keys_unavailable")
            self.keys, self.fetched = parse_jwks(object_json(raw)), self.clock()
        except (AcquisitionError, ProviderError, httpx.HTTPError, ValueError):
            raise AccessRejected("keys_unavailable") from None

    async def _key(self, kid: str) -> rsa.RSAPublicKey:
        async with self._lock:
            now = self.clock()
            fresh = self.fetched is not None and now - self.fetched < KEYS_FRESH_S
            if not (fresh and kid in self.keys) and (
                self.attempted is None or now - self.attempted >= KEYS_MIN_REFRESH_S
            ):
                self.attempted = now
                try:
                    await self._refresh()
                except AccessRejected:
                    pass  # a recent cached key may still be used below
            key = self.keys.get(kid)
            if key is None or self.fetched is None or now - self.fetched >= KEYS_STALE_MAX_S:
                raise AccessRejected("unknown_key")
            return key

    async def verify(self, headers: list[str]) -> AccessIdentity:
        """``headers`` is every value of the assertion header; exactly one well-formed token is accepted."""
        if len(headers) != 1 or not TOKEN.fullmatch(headers[0]):
            raise AccessRejected("token_missing")
        token = headers[0]
        head_b64, body_b64, signature_b64 = token.split(".")
        try:
            header: Any = json.loads(_segment(head_b64))
            claims: Any = json.loads(_segment(body_b64))
            signature = _segment(signature_b64)
        except (ValueError, UnicodeDecodeError):
            raise AccessRejected("token_malformed") from None
        if not isinstance(header, dict) or not isinstance(claims, dict):
            raise AccessRejected("token_malformed")
        if header.get("alg") != "RS256" or "crit" in header or header.get("typ", "JWT") != "JWT":
            raise AccessRejected("token_algorithm")
        kid = header.get("kid")
        if not isinstance(kid, str) or not KID_SHAPE.fullmatch(kid):
            raise AccessRejected("token_malformed")
        key = await self._key(kid)
        try:
            key.verify(signature, f"{head_b64}.{body_b64}".encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
        except InvalidSignature:
            raise AccessRejected("token_signature") from None
        now = self.clock()
        audience = claims.get("aud")
        audiences = audience if isinstance(audience, list) else [audience]
        if self.policy.audience not in audiences:
            raise AccessRejected("token_audience")
        if claims.get("iss") != self.policy.team_domain:
            raise AccessRejected("token_issuer")
        expires, issued, not_before = claims.get("exp"), claims.get("iat"), claims.get("nbf", claims.get("iat"))
        if not all(type(value) in (int, float) for value in (expires, issued, not_before)):
            raise AccessRejected("token_time")
        if not (expires > now and not_before <= now + SKEW_S and issued <= now + SKEW_S and issued < expires):
            raise AccessRejected("token_time")
        if claims.get("type", "app") != "app" or "common_name" in claims:
            raise AccessRejected("token_type")  # service tokens never act as the owner
        email = claims.get("email")
        # ASCII only and ASCII case-insensitive, identical to the Worker: no Unicode folding of lookalike characters.
        if not isinstance(email, str) or not ASCII_CLAIM.fullmatch(email) or email.lower() != self.policy.owner_email.lower():
            raise AccessRejected("token_subject")
        return AccessIdentity(self.policy.owner_email, float(expires), float(issued))
