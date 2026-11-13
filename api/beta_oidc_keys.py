"""Pinned provider metadata and a rotating JWKS key source for the owner relying party (AUTH-2a, offline).

Everything here goes through ``mock_response``, which accepts only an in-process ``httpx.MockTransport``: this module
cannot reach a network. A real transport, the owner's issuer and the first real login are a separate, separately
authorized increment (AUTH-2b). Nothing here enables real promotion.
"""

from __future__ import annotations

import asyncio
import base64
import re
import time
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives.asymmetric import rsa

from src.interop.epic.beta_acquisition import AcquisitionError, mock_response, object_json

JWKS_FRESH_S = 3600  # a cached key set older than this is refreshed before use
JWKS_STALE_MAX_S = 24 * 3600  # when a refresh fails or is throttled, a cached key is usable for at most this long
JWKS_MIN_REFRESH_S = 60  # at most one fetch per window, success or failure, so unknown key ids cannot drive traffic
JWKS_MAX_KEYS = 16
JWKS_LIMIT = 64 * 1024
DISCOVERY_LIMIT = 32 * 1024
KID_SHAPE = re.compile(r"[A-Za-z0-9._:=-]{1,128}")
BASE64URL = re.compile(r"[A-Za-z0-9_-]+")
URL_SHAPE = re.compile(r"[\x21-\x7e]{1,512}")


class ProviderError(ValueError):
    """Provider metadata or a signing key was refused. The code is stable and carries no provider content."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _https(value: object) -> str:
    """An absolute https URL: no credentials, query, fragment, backslash, dot segment or bad port."""
    if not isinstance(value, str) or not URL_SHAPE.fullmatch(value) or "\\" in value:
        raise ProviderError("provider_metadata")
    try:
        parts = urlsplit(value)
        parts.port  # noqa: B018 - raises ValueError for an invalid port
    except ValueError:
        raise ProviderError("provider_metadata") from None
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or any(segment in {".", ".."} for segment in parts.path.split("/"))
    ):
        raise ProviderError("provider_metadata")
    return value


@dataclass(frozen=True)
class ProviderMetadata:
    """The owner's identity provider endpoints. Frozen: nothing changes them after construction.

    The authorization and token endpoints may live on a different host than the issuer (managed-login providers do
    this), so they are validated and, when discovery is used, compared with the pinned values rather than assumed.
    """

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str

    def __post_init__(self) -> None:
        if _https(self.issuer).endswith("/"):
            raise ProviderError("provider_metadata")
        for endpoint in (self.authorization_endpoint, self.token_endpoint, self.jwks_uri):
            _https(endpoint)

    @classmethod
    def from_discovery(cls, document: dict[str, object], issuer: str) -> ProviderMetadata:
        """Build from an OpenID discovery document. The document's own ``issuer`` must equal the configured one."""
        if document.get("issuer") != issuer:
            raise ProviderError("issuer_mismatch")
        algorithms = document.get("id_token_signing_alg_values_supported")
        if algorithms is not None and (not isinstance(algorithms, list) or "RS256" not in algorithms):
            raise ProviderError("provider_metadata")
        methods = document.get("code_challenge_methods_supported")
        if methods is not None and (not isinstance(methods, list) or "S256" not in methods):
            raise ProviderError("provider_metadata")
        return cls(
            issuer,
            document.get("authorization_endpoint"),  # type: ignore[arg-type]  # validated in __post_init__
            document.get("token_endpoint"),  # type: ignore[arg-type]
            document.get("jwks_uri"),  # type: ignore[arg-type]
        )


async def discover(
    transport: httpx.MockTransport, issuer: str, *, pinned: ProviderMetadata | None = None
) -> ProviderMetadata:
    """Fetch ``{issuer}/.well-known/openid-configuration``. With ``pinned``, any difference is refused."""
    if _https(issuer).endswith("/"):
        raise ProviderError("provider_metadata")
    request = httpx.Request(
        "GET",
        f"{issuer}/.well-known/openid-configuration",
        headers={"Accept-Encoding": "identity", "Accept": "application/json"},
    )
    try:
        status, _, raw = await mock_response(transport, request, seconds=5, limit=DISCOVERY_LIMIT)
        if status != 200:
            raise ProviderError("discovery_failed")
        found = ProviderMetadata.from_discovery(object_json(raw), issuer)
    except (AcquisitionError, httpx.HTTPError):
        raise ProviderError("discovery_failed") from None
    if pinned is not None and found != pinned:
        raise ProviderError("metadata_changed")
    return found


def _integer(value: object) -> int:
    if not isinstance(value, str) or not BASE64URL.fullmatch(value) or len(value) > 2048:
        raise ProviderError("jwks_invalid")
    raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    if not raw or (len(raw) > 1 and raw[0] == 0):
        raise ProviderError("jwks_invalid")
    return int.from_bytes(raw, "big")


def _public_key(jwk: dict[str, object]) -> tuple[str, rsa.RSAPublicKey]:
    kid = jwk.get("kid")
    if not isinstance(kid, str) or not KID_SHAPE.fullmatch(kid):
        raise ProviderError("jwks_invalid")
    if jwk.get("alg") not in (None, "RS256"):
        raise ProviderError("jwks_invalid")
    modulus, exponent = _integer(jwk.get("n")), _integer(jwk.get("e"))
    if exponent != 65537 or not 2048 <= modulus.bit_length() <= 8192:
        raise ProviderError("jwks_invalid")
    try:
        return kid, rsa.RSAPublicNumbers(exponent, modulus).public_key()
    except ValueError:
        raise ProviderError("jwks_invalid") from None


def parse_jwks(document: dict[str, object]) -> dict[str, rsa.RSAPublicKey]:
    """Signature keys from a JWKS document. Keys of other types or ``use`` values are ignored; a malformed or
    ambiguous (duplicate ``kid``) RSA signature key refuses the whole document."""
    entries = document.get("keys")
    if not isinstance(entries, list) or not 1 <= len(entries) <= JWKS_MAX_KEYS:
        raise ProviderError("jwks_invalid")
    keys: dict[str, rsa.RSAPublicKey] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ProviderError("jwks_invalid")
        if entry.get("kty") != "RSA" or entry.get("use") not in (None, "sig"):
            continue
        kid, key = _public_key(entry)
        if kid in keys:
            raise ProviderError("jwks_invalid")
        keys[kid] = key
    if not keys:
        raise ProviderError("jwks_invalid")
    return keys


class JwksKeys:
    """RS256 verification keys for one issuer, refreshed on rotation.

    A key is looked up by ``kid``. An unknown ``kid`` or a cache older than ``JWKS_FRESH_S`` triggers at most one fetch
    per ``JWKS_MIN_REFRESH_S``; a successful fetch replaces the whole set, so a key the provider withdrew stops
    working at the next refresh (within ``JWKS_FRESH_S`` when no unknown ``kid`` arrives sooner). When the provider
    cannot be reached, a cached key is still honoured, but never beyond ``JWKS_STALE_MAX_S`` after it was fetched.
    """

    def __init__(
        self,
        transport: httpx.MockTransport,
        jwks_uri: str,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.transport, self.jwks_uri, self.clock = transport, _https(jwks_uri), clock
        self.keys: dict[str, rsa.RSAPublicKey] = {}
        self.fetched: float | None = None
        self.attempted: float | None = None
        self._lock = asyncio.Lock()

    async def key_for(self, kid: str) -> rsa.RSAPublicKey:
        if not isinstance(kid, str) or not KID_SHAPE.fullmatch(kid):
            raise ProviderError("unknown_kid")
        async with self._lock:
            now = self.clock()
            if self.fetched is not None and now - self.fetched < JWKS_FRESH_S and kid in self.keys:
                return self.keys[kid]
            if self.attempted is None or now - self.attempted >= JWKS_MIN_REFRESH_S:
                self.attempted = now
                try:
                    await self._refresh(now)
                except ProviderError:
                    pass  # fall back to a recent cached key below; the failure is throttled like a success
            key = self.keys.get(kid)
            if key is None or self.fetched is None or now - self.fetched >= JWKS_STALE_MAX_S:
                raise ProviderError("unknown_kid")
            return key

    async def _refresh(self, now: float) -> None:
        request = httpx.Request(
            "GET", self.jwks_uri, headers={"Accept-Encoding": "identity", "Accept": "application/json"}
        )
        try:
            status, _, raw = await mock_response(self.transport, request, seconds=5, limit=JWKS_LIMIT)
            if status != 200:
                raise ProviderError("jwks_unavailable")
            keys = parse_jwks(object_json(raw))
        except (AcquisitionError, httpx.HTTPError):
            raise ProviderError("jwks_unavailable") from None
        self.keys, self.fetched = keys, now
