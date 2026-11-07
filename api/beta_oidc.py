"""Fixed synthetic OIDC relying party for the local MOCK Beta factory."""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlencode

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

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


@dataclass
class Session:
    csrf: str = field(repr=False)
    created: float
    used: float


@dataclass(frozen=True)
class Flow:
    state: str = field(repr=False)
    nonce: str = field(repr=False)
    verifier: str = field(repr=False)
    expires: float


class OwnerAuth:
    def __init__(self, catalog: Catalog, key: rsa.RSAPublicKey, transport: httpx.MockTransport) -> None:
        self.catalog, self.key, self.transport = catalog, key, transport
        self.clock: Callable[[], float] = catalog.clock
        self.sessions: dict[str, Session] = {}
        self.flows: dict[str, Flow] = {}

    def begin(self, previous: str | None) -> tuple[str, str]:
        now = self.clock()
        self.flows = {key: flow for key, flow in self.flows.items() if flow.expires > now and key != previous}
        if len(self.flows) >= 128:
            raise RefreshRejected(429, "login_busy", FLOW_TTL)
        browser = secrets.token_urlsafe(32)
        state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        flow = Flow(state, nonce, verifier, now + FLOW_TTL)
        self.flows[browser] = flow
        query = urlencode(
            {
                "response_type": "code",
                "client_id": AUDIENCE,
                "redirect_uri": CALLBACK,
                "scope": "openid",
                "state": flow.state,
                "nonce": flow.nonce,
                "code_challenge_method": "S256",
                "code_challenge": _b64(hashlib.sha256(flow.verifier.encode()).digest()),
            }
        )
        return browser, f"{ISSUER}/authorize?{query}"

    async def complete(self, browser: str, state: str, code: str) -> None:
        flow = self.flows.pop(browser, None)
        if (
            self.catalog.revoked("owner")
            or flow is None
            or flow.expires <= self.clock()
            or not TOKEN_SHAPE.fullmatch(state)
            or not secrets.compare_digest(flow.state, state)
            or not CODE_SHAPE.fullmatch(code)
        ):
            raise RefreshRejected(401, "login_rejected")
        request = httpx.Request(
            "POST",
            f"{ISSUER}/token",
            headers={"Accept-Encoding": "identity", "Accept": "application/json"},
            data={
                "grant_type": "authorization_code",
                "client_id": AUDIENCE,
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
            self._verify(body.get("id_token"), flow.nonce)
            if self.catalog.revoked("owner") or flow.expires <= self.clock():
                raise ValueError
        except (ValueError, InvalidSignature, AcquisitionError):
            raise RefreshRejected(401, "login_rejected") from None

    def _verify(self, token: object, nonce: str) -> None:
        if not isinstance(token, str) or len(token) > 8192:
            raise ValueError
        header, payload, signature = token.split(".")
        algorithm = object_json(_decode(header))
        if algorithm.get("alg") != "RS256" or set(algorithm) - {"alg", "typ", "kid"}:
            raise ValueError
        self.key.verify(_decode(signature), f"{header}.{payload}".encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
        claims = object_json(_decode(payload))
        now = self.clock()
        issued, expires = claims.get("iat"), claims.get("exp")
        not_before = claims.get("nbf", issued)
        if (
            claims.get("iss") != ISSUER
            or claims.get("sub") != SUBJECT
            or claims.get("aud") not in (AUDIENCE, [AUDIENCE])
            or claims.get("azp", AUDIENCE) != AUDIENCE
            or claims.get("nonce") != nonce
            or type(issued) is not int
            or type(expires) is not int
            or type(not_before) is not int
            or not issued <= now < expires
            or not_before > now
            or expires - issued > 600
        ):
            raise ValueError

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
        session = Session(secrets.token_urlsafe(32), now, now)
        self.sessions[token] = session
        return token, session
