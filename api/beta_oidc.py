"""Synthetic OIDC relying party for the local MOCK Beta factory.

Owner identity is an immutable configuration (OwnerIdentity); the default is the invented offline identity.
Step-up reauthentication is a separate, session-bound flow that grants a short, one-shot elevation. Nothing here talks
to a real identity provider, fetches keys or enables real promotion (AUTH-2 is a separate increment).
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlencode, urlsplit

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from api.beta_oidc_keys import JwksKeys, ProviderMetadata
from src.interop.epic.beta_acquisition import AcquisitionError, mock_response, object_json
from src.interop.epic.beta_catalog import Catalog, RefreshRejected

ORIGIN = "https://beta.invalid"
HOST = "beta.invalid"
ISSUER = "https://owner-idp.invalid"
AUDIENCE = "invented-beta-client"
SUBJECT = "invented-owner"
CALLBACK = f"{ORIGIN}/beta/auth/callback"
SESSION_COOKIE = "__Host-beta-session"
FLOW_COOKIE = "__Host-beta-flow"
SESSION_IDLE = 15 * 60
SESSION_ABSOLUTE = 8 * 3600
FLOW_TTL = 300
AUTH_SKEW = 5  # seconds of clock skew tolerated between this process and the identity provider
TOKEN_SHAPE = re.compile(r"[A-Za-z0-9_-]{43}")
BASE64URL = re.compile(r"[A-Za-z0-9_-]+")
CODE_SHAPE = re.compile(r"[A-Za-z0-9._~-]{1,1024}")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    if not BASE64URL.fullmatch(value):
        raise ValueError
    data = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    if _b64(data) != value:
        raise ValueError
    return data


@dataclass(frozen=True)
class OwnerIdentity:
    """Who the owner is and what assurance is required. Frozen: no code path changes it after construction."""

    issuer: str
    client_id: str
    owner_subject: str  # exactly one subject; there is no list to extend
    required_amr: frozenset[str] = frozenset({"mfa"})  # authentication-method references step-up must carry
    required_acr: str | None = None  # exact authentication-context class step-up must carry, when set
    mfa_at_login: bool = False  # also demand the above (and auth_time) from an ordinary login
    max_auth_age_s: int = 900  # ordinary login: an auth_time claim, when present or required, may be at most this old
    step_up_ttl_s: int = 120  # lifetime of one elevation

    def __post_init__(self) -> None:
        parts = urlsplit(self.issuer)
        if parts.scheme != "https" or not parts.netloc or parts.query or parts.fragment or self.issuer.endswith("/"):
            raise ValueError("owner issuer must be an https URL without a trailing slash, query or fragment")
        for value in (self.client_id, self.owner_subject):
            if not isinstance(value, str) or not value or len(value) > 255:
                raise ValueError("owner client id and subject must be non-empty strings")
        if not self.required_amr and not self.required_acr:
            raise ValueError("step-up needs at least one required MFA claim")
        if not 1 <= self.step_up_ttl_s <= 300 or not 1 <= self.max_auth_age_s <= 86400:
            raise ValueError("owner identity time limits are out of range")


INVENTED_IDENTITY = OwnerIdentity(ISSUER, AUDIENCE, SUBJECT)


@dataclass(frozen=True)
class AuthClaims:
    """Authentication-event claims taken from a verified id_token."""

    auth_time: int | None = None
    acr: str | None = None
    amr: tuple[str, ...] = ()


@dataclass(frozen=True)
class Elevation:
    """One-shot proof of recent reauthentication, bound to one session and its CSRF token."""

    expires: float
    csrf: str = field(repr=False)


@dataclass
class Session:
    csrf: str = field(repr=False)
    created: float
    used: float
    auth: AuthClaims = field(default_factory=AuthClaims)
    elevation: Elevation | None = field(default=None, repr=False)


@dataclass(frozen=True)
class Flow:
    state: str = field(repr=False)
    nonce: str = field(repr=False)
    verifier: str = field(repr=False)
    expires: float
    channel: str = "legacy-json"
    purpose: str = "login"  # "login" creates a session; "step-up" elevates one existing session
    session: str = field(default="", repr=False)  # step-up only: the session token that asked
    csrf: str = field(default="", repr=False)  # step-up only: that session's CSRF token at the time
    started: float = 0.0


class OwnerAuth:
    def __init__(
        self,
        catalog: Catalog,
        key: rsa.RSAPublicKey | None,
        transport: httpx.MockTransport,
        identity: OwnerIdentity = INVENTED_IDENTITY,
        *,
        provider: ProviderMetadata | None = None,
        keys: JwksKeys | None = None,
    ) -> None:
        """Without ``provider`` the invented in-process identity is used: ``{issuer}/authorize``, ``{issuer}/token`` and
        the one static ``key``. With ``provider`` and ``keys`` (both, and no static key) the endpoints come from the
        pinned provider metadata and id_tokens are verified against the rotating JWKS by ``kid``."""
        if (provider is None) != (keys is None):
            raise ValueError("provider metadata and a key source go together")
        if provider is None:
            if key is None:
                raise ValueError("a signing key is required")
            self.authorize_url, self.token_url = f"{identity.issuer}/authorize", f"{identity.issuer}/token"
        else:
            assert keys is not None
            if key is not None or provider.issuer != identity.issuer or keys.jwks_uri != provider.jwks_uri:
                raise ValueError("provider metadata must match the owner identity and replace the static key")
            self.authorize_url, self.token_url = provider.authorization_endpoint, provider.token_endpoint
        self.catalog, self.key, self.transport, self.identity = catalog, key, transport, identity
        self.keys = keys
        self.pending_auth = AuthClaims()  # set by a successful ordinary login, taken by create_session
        self.clock: Callable[[], float] = catalog.clock
        self.sessions: dict[str, Session] = {}
        self.flows: dict[str, Flow] = {}

    def begin(self, previous: str | None, *, channel: str = "legacy-json") -> tuple[str, str]:
        if channel not in {"legacy-json", "browser-shell"}:
            raise RefreshRejected(401, "login_rejected")
        return self._start(previous, channel, "login", "", "")

    def begin_step_up(self, token: str, previous: str | None) -> tuple[str, str]:
        """Start reauthentication for one live session: the IdP is told to authenticate again (max_age=0)."""
        session = self.session(token)
        if session is None:
            raise RefreshRejected(401, "owner_required")
        return self._start(previous, "legacy-json", "step-up", token, session.csrf)

    def _start(self, previous: str | None, channel: str, purpose: str, token: str, csrf: str) -> tuple[str, str]:
        now = self.clock()
        self.flows = {key: flow for key, flow in self.flows.items() if flow.expires > now and key != previous}
        if len(self.flows) >= 128:
            raise RefreshRejected(429, "login_busy", FLOW_TTL)
        browser = secrets.token_urlsafe(32)
        state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        flow = Flow(state, nonce, verifier, now + FLOW_TTL, channel, purpose, token, csrf, now)
        self.flows[browser] = flow
        parameters = {
            "response_type": "code",
            "client_id": self.identity.client_id,
            "redirect_uri": CALLBACK,
            "scope": "openid",
            "state": flow.state,
            "nonce": flow.nonce,
            "code_challenge_method": "S256",
            "code_challenge": _b64(hashlib.sha256(flow.verifier.encode()).digest()),
        }
        if purpose == "step-up":
            parameters |= {"prompt": "login", "max_age": "0"}
        return browser, f"{self.authorize_url}?{urlencode(parameters)}"

    async def complete(self, browser: str, state: str, code: str) -> str:
        self.pending_auth = AuthClaims()
        flow = self._take(browser, state, code, "login")
        auth = await self._exchange(flow, code)
        now = self.clock()
        stale = auth.auth_time is not None and (
            auth.auth_time > now + AUTH_SKEW or now - auth.auth_time > self.identity.max_auth_age_s
        )
        if stale or (self.identity.mfa_at_login and (auth.auth_time is None or not self._mfa(auth))):
            raise RefreshRejected(401, "login_rejected")
        self.pending_auth = auth
        return flow.channel

    async def complete_step_up(self, browser: str, state: str, code: str, token: str) -> None:
        """Grant one elevation to the session that asked, only if the IdP proves a new MFA authentication."""
        flow = self._take(browser, state, code, "step-up")
        session = self.session(token)
        if (
            session is None
            or not secrets.compare_digest(flow.session, token)
            or not secrets.compare_digest(flow.csrf, session.csrf)
        ):
            raise RefreshRejected(401, "step_up_rejected")
        auth = await self._exchange(flow, code, rejection="step_up_rejected")
        now = self.clock()
        if (
            auth.auth_time is None
            or not flow.started - AUTH_SKEW <= auth.auth_time <= now + AUTH_SKEW  # authenticated after we asked
            or not self._mfa(auth)
            or self.session(token) is not session  # still the same live session after the await
        ):
            raise RefreshRejected(401, "step_up_rejected")
        session.auth = auth
        session.elevation = Elevation(now + self.identity.step_up_ttl_s, session.csrf)

    def consume_elevation(self, token: str, csrf: str) -> bool:
        """True once per elevation: it is cleared whether or not it is still valid, so it can never be replayed."""
        session = self.session(token)
        if session is None:
            return False
        elevation, session.elevation = session.elevation, None
        return (
            elevation is not None
            and self.clock() < elevation.expires
            and bool(csrf)
            and secrets.compare_digest(csrf, session.csrf)
            and secrets.compare_digest(csrf, elevation.csrf)
        )

    def _mfa(self, auth: AuthClaims) -> bool:
        identity = self.identity
        return set(identity.required_amr) <= set(auth.amr) and (
            identity.required_acr is None or auth.acr == identity.required_acr
        )

    def _take(self, browser: str, state: str, code: str, purpose: str) -> Flow:
        flow = self.flows.pop(browser, None)  # single use, whatever happens next
        rejection = "login_rejected" if purpose == "login" else "step_up_rejected"
        if (
            self.catalog.revoked("owner")
            or flow is None
            or flow.purpose != purpose
            or flow.channel not in {"legacy-json", "browser-shell"}
            or flow.expires <= self.clock()
            or not TOKEN_SHAPE.fullmatch(state)
            or not secrets.compare_digest(flow.state, state)
            or not CODE_SHAPE.fullmatch(code)
        ):
            raise RefreshRejected(401, rejection)
        return flow

    async def _exchange(self, flow: Flow, code: str, *, rejection: str = "login_rejected") -> AuthClaims:
        request = httpx.Request(
            "POST",
            self.token_url,
            headers={"Accept-Encoding": "identity", "Accept": "application/json"},
            data={
                "grant_type": "authorization_code",
                "client_id": self.identity.client_id,
                "redirect_uri": CALLBACK,
                "code": code,
                "code_verifier": flow.verifier,
            },
        )
        try:
            status, _, raw = await mock_response(self.transport, request, seconds=5, limit=32 * 1024)
            if status != 200:
                raise ValueError
            body = object_json(raw)
            token = body.get("id_token")
            auth = self._verify(token, flow.nonce, await self._signing_key(token))
            if self.catalog.revoked("owner") or flow.expires <= self.clock():
                raise ValueError
        except (ValueError, InvalidSignature, AcquisitionError):
            raise RefreshRejected(401, rejection) from None
        return auth

    @staticmethod
    def _header(token: object) -> dict[str, object]:
        """The protected header of a compact JWS, only if it names RS256 and nothing that could redirect key lookup."""
        if not isinstance(token, str) or len(token) > 8192:
            raise ValueError
        header, _, _ = token.split(".")
        algorithm = object_json(_decode(header))
        if algorithm.get("alg") != "RS256" or set(algorithm) - {"alg", "typ", "kid"}:
            raise ValueError
        return algorithm

    async def _signing_key(self, token: object) -> rsa.RSAPublicKey:
        if self.keys is None:
            assert self.key is not None
            return self.key
        kid = self._header(token).get("kid")  # algorithm and header shape are checked before any key lookup
        if not isinstance(kid, str):
            raise ValueError
        return await self.keys.key_for(kid)

    def _verify(self, token: object, nonce: str, key: rsa.RSAPublicKey) -> AuthClaims:
        self._header(token)
        assert isinstance(token, str)
        header, payload, signature = token.split(".")
        key.verify(_decode(signature), f"{header}.{payload}".encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
        claims = object_json(_decode(payload))
        now = self.clock()
        issued, expires = claims.get("iat"), claims.get("exp")
        not_before = claims.get("nbf", issued)
        identity = self.identity
        if (
            claims.get("iss") != identity.issuer
            or claims.get("sub") != identity.owner_subject
            or claims.get("aud") not in (identity.client_id, [identity.client_id])
            or claims.get("azp", identity.client_id) != identity.client_id
            or claims.get("nonce") != nonce
            or type(issued) is not int
            or type(expires) is not int
            or type(not_before) is not int
            or not issued <= now < expires
            or not_before > now
            or expires - issued > 600
        ):
            raise ValueError
        auth_time, acr, amr = claims.get("auth_time"), claims.get("acr"), claims.get("amr") or []
        if (
            (auth_time is not None and (type(auth_time) is not int or auth_time < 0))
            or (acr is not None and (not isinstance(acr, str) or len(acr) > 64))
            or not isinstance(amr, list)
            or len(amr) > 8
            or any(not isinstance(item, str) or not 0 < len(item) <= 32 for item in amr)
        ):
            raise ValueError
        return AuthClaims(auth_time, acr, tuple(amr))

    def session(self, token: str, *, touch: bool = False) -> Session | None:
        session = self.sessions.get(token)
        if session is None or self.catalog.revoked("owner"):
            return None
        now = self.clock()
        if now - session.used >= SESSION_IDLE or now - session.created >= SESSION_ABSOLUTE:
            self.sessions.pop(token, None)
            return None
        if touch:
            session.used = now
        return session

    def create_session(self, previous: str) -> tuple[str, Session]:
        self.sessions.pop(previous, None)
        for token in tuple(self.sessions):
            self.session(token)
        if len(self.sessions) >= 64:
            self.sessions.pop(next(iter(self.sessions)))
        now = self.clock()
        token = secrets.token_urlsafe(32)
        session = Session(secrets.token_urlsafe(32), now, now, auth=self.pending_auth)
        self.pending_auth = AuthClaims()
        self.sessions[token] = session
        return token, session
