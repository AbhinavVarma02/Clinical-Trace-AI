"""Epic Backend Services OAuth 2.0 (``client_credentials`` + ``private_key_jwt``), sandbox only.

The client assertion is an RS384-signed JWT whose ``iss`` and ``sub`` are the
existing non-production client ID, whose ``aud`` is the sandbox token endpoint,
and whose lifetime is at most five minutes. The access token is kept in process
memory only: it is never written to disk, logged, printed or included in errors.
"""

from __future__ import annotations

import base64
import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from src.clinical.fhir_client import FHIRClientError
from src.interop.epic import keys, settings
from src.interop.epic.telemetry import EpicTelemetry

CLIENT_ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
MAX_ASSERTION_LIFETIME_S = 300
OAUTH_ERROR_CODES = frozenset(
    {
        "invalid_request",
        "invalid_client",
        "invalid_grant",
        "unauthorized_client",
        "unsupported_grant_type",
        "invalid_scope",
    }
)
MAX_TOKEN_RESPONSE_BYTES = 64 * 1024


class EpicAuthError(FHIRClientError):
    """Token acquisition failed. ``category`` is a fixed code; no token, assertion or body is included."""

    def __init__(self, category: str) -> None:
        super().__init__(f"Epic sandbox token request failed ({category})")
        self.category = category


def _b64url_json(value: dict[str, Any]) -> str:
    raw = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def build_client_assertion(
    private_key: rsa.RSAPrivateKey,
    client_id: str,
    *,
    kid: str | None = None,
    jku: str | None = None,
    now: float | None = None,
    lifetime_s: int = 240,
    jti: str | None = None,
) -> str:
    """Return a signed RS384 client assertion for the sandbox token endpoint."""
    if not 1 <= lifetime_s <= MAX_ASSERTION_LIFETIME_S:
        raise ValueError("the assertion lifetime must be between 1 and 300 seconds")
    issued = int(now if now is not None else time.time())
    header: dict[str, Any] = {"alg": keys.SIGNING_ALG, "typ": "JWT", "kid": kid or keys.key_id(private_key)}
    if jku:
        header["jku"] = jku
    nonce = jti or secrets.token_urlsafe(32)
    if len(nonce) > 151:
        raise ValueError("jti must be at most 151 characters")
    claims = {
        "iss": client_id,
        "sub": client_id,
        "aud": settings.TOKEN_URL,
        "jti": nonce,
        "iat": issued,
        "nbf": issued,
        "exp": issued + lifetime_s,
    }
    signing_input = f"{_b64url_json(header)}.{_b64url_json(claims)}"
    signature = private_key.sign(signing_input.encode("ascii"), padding.PKCS1v15(), hashes.SHA384())
    return f"{signing_input}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode('ascii')}"


@dataclass(frozen=True)
class AccessToken:
    value: str = field(repr=False)
    expires_at: float
    scopes: tuple[str, ...] = ()

    def __repr__(self) -> str:  # never reveal the token value
        return f"AccessToken(expires_at={self.expires_at:.0f}, scopes={len(self.scopes)})"


class EpicTokenProvider:
    """Obtains and caches one sandbox access token; refreshes it shortly before expiry."""

    def __init__(
        self,
        client_id: str,
        private_key: rsa.RSAPrivateKey,
        *,
        jku: str | None = None,
        timeout_s: float = 10.0,
        refresh_skew_s: float = 60.0,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.time,
        telemetry: EpicTelemetry | None = None,
    ) -> None:
        self._client_id = client_id
        self._private_key = private_key
        self._kid = keys.key_id(private_key)
        self._jku = jku
        self._skew = refresh_skew_s
        self._clock = clock
        self._lock = threading.Lock()
        self._token: AccessToken | None = None
        self.telemetry = telemetry or EpicTelemetry()
        self._http = httpx.Client(
            timeout=timeout_s,
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            headers={"Accept": "application/json", "User-Agent": settings.USER_AGENT},
        )

    def __repr__(self) -> str:
        return f"EpicTokenProvider(kid={self._kid}, cached={self._token is not None})"

    @property
    def kid(self) -> str:
        return self._kid

    def close(self) -> None:
        self._http.close()

    def invalidate(self) -> None:
        with self._lock:
            self._token = None

    def token(self) -> AccessToken:
        with self._lock:
            if self._token is None or self._token.expires_at - self._skew <= self._clock():
                self._token = self._request()
            return self._token

    def _request(self) -> AccessToken:
        with self.telemetry.operation("token") as op:
            assertion = build_client_assertion(
                self._private_key, self._client_id, kid=self._kid, jku=self._jku, now=self._clock()
            )
            form = {
                "grant_type": "client_credentials",
                "client_assertion_type": CLIENT_ASSERTION_TYPE,
                "client_assertion": assertion,
            }
            try:
                response = self._http.post(settings.TOKEN_URL, data=form)
            except httpx.TimeoutException:
                op.error_type = "timeout"
                raise EpicAuthError("timeout") from None
            except httpx.HTTPError:
                op.error_type = "network"
                raise EpicAuthError("network") from None
            op.status_code = response.status_code
            category = self._failure_category(response)
            if category:
                op.error_type = category
                raise EpicAuthError(category)
            token = self._parse(response)
            if token is None:
                op.error_type = "malformed"
                raise EpicAuthError("malformed")
            op.ok = True
            return token

    @staticmethod
    def _failure_category(response: httpx.Response) -> str | None:
        status = response.status_code
        if status == 200:
            return None
        if 300 <= status < 400:
            return "redirect_refused"
        if status == 429:
            return "throttled"
        if status >= 500:
            return "http_5xx"
        try:
            code = response.json().get("error")
        except (ValueError, AttributeError):
            code = None
        return code if code in OAUTH_ERROR_CODES else "http_4xx"

    def _parse(self, response: httpx.Response) -> AccessToken | None:
        if len(response.content) > MAX_TOKEN_RESPONSE_BYTES:
            return None
        try:
            body = response.json()
        except ValueError:
            return None
        if not isinstance(body, dict):
            return None
        value, token_type, expires_in = body.get("access_token"), body.get("token_type"), body.get("expires_in")
        if not isinstance(value, str) or not value.strip() or str(token_type).lower() != "bearer":
            return None
        if isinstance(expires_in, bool) or not isinstance(expires_in, (int, float)) or expires_in <= 0:
            return None
        scope = body.get("scope")
        scopes = tuple(sorted(scope.split())) if isinstance(scope, str) else ()
        return AccessToken(value=value, expires_at=self._clock() + float(expires_in), scopes=scopes)
