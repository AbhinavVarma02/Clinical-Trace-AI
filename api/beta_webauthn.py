"""Local-owner WebAuthn verifier, never an authenticator client.

Verification is delegated to python-fido2. One owner/key and controlled out-of-band enrollment. Optional
durable public state; ceremonies/sessions/elevations remain memory-only. Genuine-purpose requests require
their bound durable store and cannot create bearer sessions. Actual trust/enrollment/LIVE need approval.
"""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlsplit

from fido2.attestation import Attestation
from fido2.server import Fido2Server
from fido2.utils import websafe_encode
from fido2.webauthn import (
    AttestationConveyancePreference,
    AttestationObject,
    AttestedCredentialData,
    AuthenticationResponse,
    AuthenticatorData,
    AuthenticatorAttachment,
    CredentialCreationOptions,
    CredentialRequestOptions,
    PublicKeyCredentialRpEntity,
    PublicKeyCredentialUserEntity,
    RegistrationResponse,
    UserVerificationRequirement,
)

from api.beta_oidc import SESSION_ABSOLUTE, SESSION_COOKIE, SESSION_IDLE, TOKEN_SHAPE, AuthClaims, Elevation, Session
from api.beta_webauthn_store import GENUINE_PURPOSE, PURPOSE, OwnerCredentialStore, OwnerState
from src.interop.epic.beta_acquisition import object_json
from src.interop.epic.beta_catalog import RefreshRejected

# The vendor logs raw credential IDs at INFO/DEBUG. No application authentication identifiers may reach logs.
logging.getLogger("fido2.server").disabled = True
CEREMONY_S = 120
ENROLLMENT_S = 60
STEP_UP_S = 120
MAX_PENDING = 16
LOCAL_BIND = "127.0.0.1"
# Explicit offline contract, not proof of hostname reservation, browser trust or genuine provisioning.
# Historical cookie seams have no port isolation and stay synthetic-only; genuine requests use no cookies.
OWNER_ORIGIN = "https://owner.clinical-trace.localhost:8443"
OWNER_RP_ID = "owner.clinical-trace.localhost"
OWNER_SESSION_COOKIE = "__Host-ct-owner-session"


@dataclass(frozen=True)
class LocalOwnerPolicy:
    origin: str
    owner_handle: bytes = field(repr=False)
    purpose: str = PURPOSE  # distinct genuine purpose is accepted only at the pinned owner origin; no conversion

    def __post_init__(self) -> None:
        parts = urlsplit(self.origin)
        try:
            port = parts.port
        except ValueError:
            raise ValueError("invalid local HTTPS origin") from None
        if (
            parts.scheme != "https"
            or (
                self.origin != OWNER_ORIGIN
                and (
                    parts.hostname != "localhost"
                    or parts.netloc != ("localhost" if port in (None, 443) else f"localhost:{port}")
                )
            )
            or self.origin != f"https://{parts.netloc}"
            or (port is not None and not 1 <= port <= 65535)
            or parts.path
            or parts.query
            or parts.fragment
            or type(self.owner_handle) is not bytes
            or not 16 <= len(self.owner_handle) <= 64
            or self.purpose not in {PURPOSE, GENUINE_PURPOSE}
            or (self.purpose == GENUINE_PURPOSE and self.origin != OWNER_ORIGIN)
        ):
            raise ValueError("one explicit local HTTPS origin, purpose and approved owner handle are required")

    @property
    def rp_id(self) -> str:
        return OWNER_RP_ID if self.origin == OWNER_ORIGIN else "localhost"

    @property
    def session_cookie(self) -> str:
        # Retain historical localhost fixtures; no normal Beta/MOCK cookie contract changes.
        return OWNER_SESSION_COOKIE if self.origin == OWNER_ORIGIN else SESSION_COOKIE

    @property
    def authority(self) -> str:
        return urlsplit(self.origin).netloc


@dataclass(frozen=True)
class Ceremony:
    purpose: str
    state: dict[str, Any] = field(repr=False)
    browser: str = field(repr=False)
    expires: float
    session: str = field(default="", repr=False)
    csrf: str = field(default="", repr=False)


def _attestation(statement: AttestationObject, client_hash: bytes) -> None:
    # Fido2Server otherwise ignores attestation. Format-specific cryptography remains entirely in the library.
    Attestation.for_type(statement.fmt)().verify(statement.att_stmt, statement.auth_data, client_hash)


class LocalOwnerAuth:
    def __init__(
        self,
        policy: LocalOwnerPolicy,
        *,
        clock: Callable[[], float] = time.time,
        authorize_enrollment: Callable[[], bool] | None = None,
        store: OwnerCredentialStore | None = None,
    ) -> None:
        if policy.purpose == GENUINE_PURPOSE and (type(store) is not OwnerCredentialStore or store.policy != policy):
            raise ValueError("genuine owner authentication requires its bound durable store")
        self.policy, self.clock, self._authorize_enrollment = policy, clock, authorize_enrollment
        self._server = Fido2Server(
            PublicKeyCredentialRpEntity(id=policy.rp_id, name="Clinical-Trace local owner"),
            attestation=AttestationConveyancePreference.DIRECT,
            verify_origin=lambda origin: secrets.compare_digest(origin, policy.origin),
            verify_attestation=_attestation,
        )
        self._server.timeout = CEREMONY_S * 1000  # type: ignore[assignment]  # vendor infers None for this configurable field
        self._credential: AttestedCredentialData | None = None
        self._counter = 0
        self._revoked = False
        self._grants: dict[str, float] = {}
        self._pending: dict[str, Ceremony] = {}
        self.sessions: dict[str, Session] = {}
        self._store, self._state, self._storage_blocked = store, OwnerState(), False
        if store is not None:
            try:
                if store.policy != policy:
                    raise ValueError
                self._state = store.load()
                if self._state.credential is not None:
                    key = AttestedCredentialData(self._state.credential)
                    if (
                        bytes(key) != self._state.credential
                        or not 1 <= len(key.credential_id) <= 1023
                        or key.public_key.get(3) not in {p.alg for p in self._server.allowed_algorithms}
                    ):
                        raise ValueError
                    self._credential = key
                self._counter, self._revoked = self._state.counter, self._state.revoked
            except Exception:
                self._storage_failure()

    def _storage_failure(self) -> None:
        self._storage_blocked = True
        self.sessions.clear()
        self._pending.clear()
        self._grants.clear()
        raise RefreshRejected(503, "owner_storage_unavailable") from None

    def _persist(self, operation: Callable[[OwnerState], OwnerState]) -> None:
        try:
            self._state = operation(self._state)
        except Exception:
            self._storage_failure()

    def _owner_available(self) -> None:
        if self._storage_blocked:
            self._storage_failure()
        if self._store is not None:
            try:
                if self._store.load() != self._state:
                    raise ValueError
            except Exception:
                self._storage_failure()
        if self._revoked:
            raise RefreshRejected(401, "owner_revoked")

    def issue_enrollment_grant(self) -> str:
        """Out-of-band provisioner only: no HTTP route can issue this capability. Default authorization is deny."""
        self._owner_available()
        if self._credential is not None:
            raise RefreshRejected(409, "owner_already_enrolled")
        if self._authorize_enrollment is None or self._authorize_enrollment() is not True:
            raise RefreshRejected(403, "enrollment_not_authorized")
        now = self.clock()
        self._grants = {grant: expiry for grant, expiry in self._grants.items() if expiry > now}
        if self._grants or any(flow.purpose == "register" and flow.expires > now for flow in self._pending.values()):
            raise RefreshRejected(409, "enrollment_pending")
        grant = secrets.token_urlsafe(32)
        self._grants[grant] = now + ENROLLMENT_S
        return grant

    def _begin(self, purpose: str, browser: str, token: str = "", csrf: str = "") -> dict[str, Any]:
        self._owner_available()
        if not isinstance(browser, str) or not TOKEN_SHAPE.fullmatch(browser):
            raise RefreshRejected(400, "browser_binding_required")
        now = self.clock()
        self._pending = {key: flow for key, flow in self._pending.items() if flow.expires > now}
        if len(self._pending) >= MAX_PENDING:
            raise RefreshRejected(429, "authentication_busy")
        options: CredentialCreationOptions | CredentialRequestOptions
        if purpose == "register":
            options, state = self._server.register_begin(
                # displayName is a REQUIRED WebAuthn JSON member; browsers reject the options without it.
                PublicKeyCredentialUserEntity(
                    id=self.policy.owner_handle, name="approved-local-owner", display_name="Approved local owner"
                ),
                credentials=[],
                user_verification=UserVerificationRequirement.REQUIRED,
                authenticator_attachment=AuthenticatorAttachment.PLATFORM
                if self.policy.purpose == GENUINE_PURPOSE
                else None,
            )
        else:
            if self._credential is None:
                raise RefreshRejected(403, "owner_not_enrolled")
            options, state = self._server.authenticate_begin(
                [self._credential], user_verification=UserVerificationRequirement.REQUIRED
            )
        flow_id = secrets.token_urlsafe(32)
        self._pending[flow_id] = Ceremony(purpose, state, browser, now + CEREMONY_S, token, csrf)
        return {"flow_id": flow_id, **dict(options)}

    def begin_registration(self, grant: str, browser: str) -> dict[str, Any]:
        self._owner_available()
        if self._credential is not None:
            raise RefreshRejected(409, "owner_already_enrolled")
        expiry = self._grants.pop(grant, 0)  # one use, including an unsuccessful begin
        if expiry <= self.clock():
            raise RefreshRejected(403, "enrollment_not_authorized")
        return self._begin("register", browser)

    def _take(self, flow_id: str, browser: str, purpose: str) -> Ceremony:
        flow = self._pending.pop(flow_id, None)  # consumed before validation, including wrong browser/purpose
        self._owner_available()
        if (
            flow is None
            or flow.purpose != purpose
            or flow.expires <= self.clock()
            or not secrets.compare_digest(flow.browser, browser)
        ):
            raise RefreshRejected(401, "ceremony_rejected")
        return flow

    @staticmethod
    def _context(response: RegistrationResponse | AuthenticationResponse, data: AuthenticatorData) -> None:
        client = object_json(bytes(response.response.client_data))
        if client.get("crossOrigin", False) is not False or "topOrigin" in client:
            raise ValueError("cross-origin ceremony")
        if data.is_backup_eligible() or data.is_backed_up():
            raise ValueError("device-bound credential required")

    def complete_registration(self, flow_id: str, browser: str, credential: dict[str, Any]) -> None:
        flow = self._take(flow_id, browser, "register")
        if self._credential is not None:
            raise RefreshRejected(409, "owner_already_enrolled")
        try:
            response = RegistrationResponse.from_dict(credential)
            data = self._server.register_complete(flow.state, response)
            self._context(response, data)
            key = data.credential_data
            if (
                key is None
                or not 1 <= len(key.credential_id) <= 1023
                or not secrets.compare_digest(key.credential_id, response.raw_id)
                or key.public_key.get(3) not in {parameter.alg for parameter in self._server.allowed_algorithms}
            ):
                raise ValueError
            if flow.expires <= self.clock() or self._revoked:
                raise ValueError
        except Exception:
            raise RefreshRejected(401, "registration_rejected") from None
        store = self._store
        if store is not None:
            if self.policy.purpose == GENUINE_PURPOSE:
                self._persist(
                    lambda state: store.enroll(
                        state, bytes(key), data.counter, valid=lambda: flow.expires > self.clock() and not self._revoked
                    )
                )
            else:
                self._persist(lambda state: store.enroll(state, bytes(key), data.counter))
        self._credential, self._counter = key, data.counter
        self._grants.clear()
        self._pending.clear()

    def begin_login(self, browser: str) -> dict[str, Any]:
        if self.policy.purpose == GENUINE_PURPOSE:
            raise RefreshRejected(403, "bearer_sessions_disabled")
        return self._begin("login", browser)

    def begin_request(self, context: bytes) -> dict[str, Any]:
        """A random signed challenge maps to one server-selected request context, never a bearer session."""
        if type(context) is not bytes or len(context) != 32:
            raise RefreshRejected(400, "request_context_required")
        return self._begin("protected-request", websafe_encode(context))

    def complete_request(self, flow_id: str, context: bytes, credential: dict[str, Any]) -> float:
        flow = self._take(flow_id, websafe_encode(context), "protected-request")
        self._authenticate(flow, credential)
        self._owner_available()
        return flow.expires

    def request_current(self, expires: float) -> bool:
        self._owner_available()
        return self._credential is not None and self.clock() < expires

    def _authenticate(self, flow: Ceremony, credential: dict[str, Any]) -> None:
        try:
            if self._credential is None:
                raise ValueError
            response = AuthenticationResponse.from_dict(credential)
            self._server.authenticate_complete(flow.state, [self._credential], response)
            data = response.response.authenticator_data
            self._context(response, data)
            handle = response.response.user_handle
            if handle is not None and not secrets.compare_digest(handle, self.policy.owner_handle):
                raise ValueError
            if (self._counter or data.counter) and data.counter <= self._counter:
                raise ValueError("counter rollback")
            if flow.expires <= self.clock() or self._revoked:
                raise ValueError
        except Exception:
            raise RefreshRejected(401, "authentication_rejected") from None
        store = self._store
        if store is not None:
            self._persist(lambda state: store.advance(state, data.counter))
        self._counter = data.counter
        if flow.expires <= self.clock():  # durable I/O must not turn an expired proof into a session/elevation
            raise RefreshRejected(401, "authentication_rejected")

    def complete_login(self, flow_id: str, browser: str, credential: dict[str, Any]) -> tuple[str, Session]:
        if self.policy.purpose == GENUINE_PURPOSE:
            raise RefreshRejected(403, "bearer_sessions_disabled")
        flow = self._take(flow_id, browser, "login")
        self._authenticate(flow, credential)
        self.sessions.clear()  # one owner; a new verified login replaces every previous session/elevation
        self._pending.clear()
        now, token = self.clock(), secrets.token_urlsafe(32)
        session = Session(secrets.token_urlsafe(32), now, now, AuthClaims(auth_time=int(now), amr=("webauthn", "uv")))
        self.sessions[token] = session
        return token, session

    def session(self, token: str, *, touch: bool = False) -> Session | None:
        if self.policy.purpose == GENUINE_PURPOSE:
            return None
        try:
            self._owner_available()  # durable revocation, storage health and OS-owner binding apply to every use
        except RefreshRejected:
            return None
        session = self.sessions.get(token)
        if session is None or self._revoked:
            return None
        now = self.clock()
        if now - session.used >= SESSION_IDLE or now - session.created >= SESSION_ABSOLUTE:
            self.sessions.pop(token, None)
            return None
        if touch:
            session.used = now
        return session

    def require_session(self, token: str, csrf: str) -> Session:
        session = self.session(token)
        if session is None:
            raise RefreshRejected(401, "owner_required")
        if not TOKEN_SHAPE.fullmatch(csrf) or not secrets.compare_digest(session.csrf, csrf):
            raise RefreshRejected(403, "csrf_rejected")
        return session

    def begin_step_up(self, token: str, csrf: str, browser: str) -> dict[str, Any]:
        session = self.require_session(token, csrf)
        session.elevation = None
        return self._begin("step-up", browser, token, csrf)

    def complete_step_up(self, flow_id: str, browser: str, credential: dict[str, Any], token: str, csrf: str) -> None:
        flow = self._take(flow_id, browser, "step-up")
        session = self.require_session(token, csrf)
        if not secrets.compare_digest(flow.session, token) or not secrets.compare_digest(flow.csrf, csrf):
            raise RefreshRejected(401, "ceremony_rejected")
        self._authenticate(flow, credential)
        if self.session(token) is not session:
            raise RefreshRejected(401, "owner_required")
        session.elevation = Elevation(self.clock() + STEP_UP_S, csrf)

    def consume_elevation(self, token: str, csrf: str) -> bool:
        session = self.session(token)
        if session is None:
            return False
        elevation, session.elevation = session.elevation, None
        return (
            elevation is not None
            and self.clock() < elevation.expires
            and secrets.compare_digest(csrf, session.csrf)
            and secrets.compare_digest(csrf, elevation.csrf)
        )

    def logout(self, token: str, csrf: str) -> None:
        self.require_session(token, csrf)
        self.sessions.pop(token, None)
        self._pending.clear()

    def revoke_owner(self) -> None:
        self._revoked = True
        self.sessions.clear()
        self._pending.clear()
        self._grants.clear()
        if self._store is not None:
            self._persist(self._store.revoke)
