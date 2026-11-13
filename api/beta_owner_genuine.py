"""Default-disabled, cookie-free genuine owner integration. No launcher, IdP or acquisition worker.

Each protected result consumes a fresh WebAuthn UP+UV proof and is returned on that same request.
The only clinical operation is one fixed allowlisted patient's existing verified LIVE record projection.
Provisioning/enrollment remain local-console actions; real execution is a separate owner approval.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import secrets
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from api.beta_browser import BrowserAssets, static_namespace
from api.beta_live_record import LiveEvidenceBinding, live_record_view
from api.beta_owner_console import confirm_owner_action
from api.beta_webauthn import ENROLLMENT_S, MAX_PENDING, OWNER_ORIGIN, LocalOwnerAuth, LocalOwnerPolicy
from api.beta_webauthn_store import GENUINE_PURPOSE, OwnerCredentialStore
from src.interop.epic.beta_acquisition import object_json
from src.interop.epic.beta_catalog import RefreshRejected
from src.interop.epic.beta_live_generations import LiveGenerations

BASE = "/owner/genuine"
PROFILE = {
    "x-owner-client": "genuine-webauthn-v1",
    "accept": "application/json",
    "sec-fetch-site": "same-origin",
    "sec-fetch-mode": "same-origin",
    "sec-fetch-dest": "empty",
}
DISPLAY_S = 30


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def provision_owner_store(
    root: Path, principal: str, *, enabled: bool = False, policy_accepted: bool = False
) -> OwnerCredentialStore:
    """NEW state only, accepted attended-platform-UV/no-reset-or-restore policy. Never called by HTTP.

    The caller supplies the separately approved SID and private absolute path, never a request/env identity.
    No manufacturer/TPM claim. Missing/corrupt/revoked state stays unavailable; recovery has no bypass.
    """
    if enabled is not True or policy_accepted is not True or not root.is_absolute() or not principal:
        raise ValueError("genuine provisioning is disabled")
    policy = LocalOwnerPolicy(OWNER_ORIGIN, secrets.token_bytes(32), GENUINE_PURPOSE)
    fingerprint = _digest(["NEW owner store", str(root), principal, policy.owner_handle.hex(), policy.purpose])
    return OwnerCredentialStore.provision_genuine(
        root,
        policy,
        principal,
        authorize=lambda: confirm_owner_action(principal, fingerprint, enabled=True),
    )


@dataclass
class Enrollment:
    binding: str = field(repr=False)
    fingerprint: str
    expires: float
    grant: str = field(default="", repr=False)
    flow: str = field(default="", repr=False)


class GenuineOwnerApp:
    """Single event-loop/process only. No threadpool, session cookies, elevation tokens or result handles."""

    def __init__(
        self,
        store: OwnerCredentialStore,
        reads: LiveGenerations | None,
        live_reads_approved: bool,
        assets: BrowserAssets | None,
        *,
        enabled: bool = False,
        policy_accepted: bool = False,
        approved_evidence: LiveEvidenceBinding | None = None,
    ) -> None:
        if (
            enabled is not True
            or policy_accepted is not True
            or type(store) is not OwnerCredentialStore
            or store.policy.purpose != GENUINE_PURPOSE
            or store.policy.origin != OWNER_ORIGIN
            or type(live_reads_approved) is not bool
            or (
                live_reads_approved
                and (type(reads) is not LiveGenerations or type(approved_evidence) is not LiveEvidenceBinding)
            )
            or (not live_reads_approved and (reads is not None or approved_evidence is not None))
        ):
            raise ValueError("genuine durable owner integration required")
        self.policy = store.policy
        self._store, self._reads, self._live_reads_approved = store, reads, live_reads_approved
        self._approved_evidence = approved_evidence
        self._process = secrets.token_hex(32)
        self._approving = False
        self._auth = LocalOwnerAuth(store.policy, store=store, authorize_enrollment=lambda: self._approving)
        self._enrollments: dict[str, Enrollment] = {}
        self._assets = assets

    def _context(self, operation: str) -> bytes:
        target = ["POST", BASE + "/verify", "", _digest({})]
        if operation == "read":
            if (
                not self._live_reads_approved
                or self._reads is None
                or self._approved_evidence is None
                or len(self._reads.allowlist.patients) != 1
            ):
                raise RefreshRejected(403, "live_reads_disabled")
            alias = next(iter(self._reads.allowlist.patients))
            target = ["GET", f"/beta/epic/patients/{alias}/record", "", hashlib.sha256(b"").hexdigest()]
            target.append(_digest([self._reads.allowlist.get(alias), asdict(self._approved_evidence)]))
        return bytes.fromhex(
            _digest(
                [
                    "one-use-protected-request-v1",
                    operation,
                    target,
                    self._process,
                    self.policy.origin,
                    self.policy.purpose,
                    self.policy.owner_handle.hex(),
                    self._store.principal,
                    hashlib.sha256(self._auth._state.credential or b"").hexdigest(),
                ]
            )
        )

    def _pending(self, identifier: str, binding: str) -> Enrollment:
        pending = self._enrollments.pop(identifier, None)
        self._auth._owner_available()
        if (
            pending is None
            or pending.expires <= self._auth.clock()
            or not secrets.compare_digest(pending.binding, binding)
        ):
            raise RefreshRejected(401, "enrollment_rejected")
        return pending

    def approve_enrollment(self, identifier: str, fingerprint: str, *, enabled: bool = False) -> None:
        """Same-process attended console only. Neither a matching SID nor startup grants approval."""
        if enabled is not True:
            raise RefreshRejected(403, "enrollment_not_authorized")
        pending = self._enrollments.pop(identifier, None)
        self._auth._owner_available()
        if (
            pending is None
            or pending.expires <= self._auth.clock()
            or pending.fingerprint != fingerprint
            or pending.grant
            or pending.flow
        ):
            raise RefreshRejected(401, "enrollment_rejected")
        if not confirm_owner_action(self._store.principal, fingerprint, enabled=True):
            raise RefreshRejected(403, "enrollment_not_authorized")
        self._auth._owner_available()
        if pending.expires <= self._auth.clock():
            raise RefreshRejected(401, "enrollment_rejected")
        self._approving = True
        try:
            pending.grant = self._auth.issue_enrollment_grant()
        finally:
            self._approving = False
        self._enrollments[identifier] = pending

    def revoke_owner(self, *, enabled: bool = False) -> None:
        fingerprint = _digest(["PERMANENT owner revocation", self._process, self.policy.owner_handle.hex()])
        if enabled is not True or not confirm_owner_action(self._store.principal, fingerprint, enabled=True):
            raise RefreshRejected(403, "revocation_not_authorized")
        self._enrollments.clear()
        self._auth.revoke_owner()  # durable tombstone before success; errors remain refusals

    def _action(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
        self._auth._owner_available()
        now = self._auth.clock()
        if action == "operations":
            if body:
                raise RefreshRejected(400, "unexpected_payload")
            return {"actions": []}
        if action == "enroll/pending":
            if set(body) != {"binding"} or not _token(body["binding"]) or self._auth._credential is not None:
                raise RefreshRejected(400, "enrollment_rejected")
            self._enrollments = {key: value for key, value in self._enrollments.items() if value.expires > now}
            if self._enrollments or len(self._auth._pending) >= MAX_PENDING:
                raise RefreshRejected(429, "enrollment_pending")
            identifier = secrets.token_urlsafe(32)
            fingerprint = _digest(
                [
                    self._process,
                    identifier,
                    body["binding"],
                    self.policy.origin,
                    self.policy.purpose,
                    self.policy.owner_handle.hex(),
                    self._store.principal,
                ]
            )
            self._enrollments[identifier] = Enrollment(body["binding"], fingerprint, now + ENROLLMENT_S)
            return {"pending_id": identifier, "fingerprint": fingerprint}
        if action in {"enroll/options", "enroll/finish"}:
            expected = {"pending_id", "binding"} | ({"flow_id", "credential"} if action.endswith("finish") else set())
            if set(body) != expected or not all(_token(body[key]) for key in ("pending_id", "binding")):
                raise RefreshRejected(400, "unexpected_payload")
            pending = self._pending(body["pending_id"], body["binding"])
            if action.endswith("options"):
                if not pending.grant or pending.flow:
                    raise RefreshRejected(403, "enrollment_not_authorized")
                options = self._auth.begin_registration(pending.grant, pending.binding)
                pending.grant, pending.flow = "", options["flow_id"]
                flow = self._auth._pending[pending.flow]
                self._auth._pending[pending.flow] = replace(flow, expires=min(flow.expires, pending.expires))
                self._enrollments[body["pending_id"]] = pending
                return options
            if not pending.flow or pending.flow != body["flow_id"] or not isinstance(body["credential"], dict):
                raise RefreshRejected(401, "enrollment_rejected")
            self._auth.complete_registration(pending.flow, pending.binding, body["credential"])
            self._enrollments.clear()
            return {"enrolled": True}  # never logs in, creates a session or enables LIVE
        operation, _, stage = action.partition("/")
        if operation not in {"verify", "read"} or stage not in {"options", "finish"}:
            raise RefreshRejected(404, "not_found")
        context = self._context(operation)
        if stage == "options":
            if body:
                raise RefreshRejected(400, "unexpected_payload")
            return self._auth.begin_request(context)
        if (
            set(body) != {"flow_id", "credential"}
            or not _token(body["flow_id"])
            or not isinstance(body["credential"], dict)
        ):
            raise RefreshRejected(400, "unexpected_payload")
        expires = self._auth.complete_request(body["flow_id"], context, body["credential"])
        deadline = min(expires, self._auth.clock() + DISPLAY_S)

        def authorized() -> bool:
            return self._auth.request_current(deadline) and (operation != "read" or self._live_reads_approved)

        result: dict[str, Any] = {"authenticated": True}
        if operation == "read":
            assert self._reads is not None
            alias = next(iter(self._reads.allowlist.patients))
            result = live_record_view(
                self._reads, alias, authorized=authorized, genuine_only=True, approved=self._approved_evidence
            )
            result["serving"] = result["freshness"]
            result["warnings"] = [
                warning for warning in result["warnings"] if warning["code"] != "live_access_disabled"
            ]
            result["warnings"].append(
                {
                    "code": "clinical_ai_disabled",
                    "severity": "warning",
                    "message": "Clinical AI and publication remain disabled.",
                }
            )
        if not authorized():
            raise RefreshRejected(401, "owner_required")
        # Serialize INSIDE the authorization window, then recheck at the ASGI send boundary too.
        return {"result": result, "server_time": self._auth.clock(), "display_until": deadline}

    def _return_current(self, payload: dict[str, Any]) -> None:
        self._auth._owner_available()
        if "display_until" not in payload:
            return

        def authorized() -> bool:
            return self._auth.request_current(payload["display_until"])

        if not authorized():
            raise RefreshRejected(401, "owner_required")
        result = payload["result"]
        if result.get("mode") == "LIVE":
            if not self._live_reads_approved or self._reads is None or self._approved_evidence is None:
                raise RefreshRejected(403, "live_reads_disabled")

            # No awaited send may turn a revoked/expired/replaced generation into a protected result.
            def binding(manifest: dict, bundle: bytes, row: dict) -> dict:
                assert self._approved_evidence is not None
                if manifest["synthetic_transport"] is not False:
                    raise RefreshRejected(409, "genuine_evidence_required")
                return self._approved_evidence.require(manifest, row)

            current = self._reads.project_active(result["alias"], authorized, binding)
            if current != {key: result["provenance"][key] for key in current}:
                raise RefreshRejected(409, "generation_integrity")

    async def _dispatch_action(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
        """Keep the auth-only contract synchronous; bounded coordinators may await their worker here."""
        return self._action(action, body)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            return
        headers = scope.get("headers", [])
        payload: dict[str, Any] = {}

        def header(name: str) -> list[str]:
            return [value.decode("latin1") for key, value in headers if key.decode("latin1").lower() == name]

        try:
            try:
                loopback = ipaddress.ip_address(scope["client"][0]).is_loopback
            except (KeyError, TypeError, ValueError):
                loopback = False
            if not loopback or scope.get("scheme") != "https" or header("host") != [self.policy.authority]:
                raise RefreshRejected(403, "local_https_required")
            path = scope.get("path", "")
            if self._assets is not None and static_namespace(path):
                if (
                    scope.get("method") not in {"GET", "HEAD"}
                    or scope.get("query_string")
                    or scope.get("raw_path", path.encode()) != path.encode()
                    or (header("origin") and header("origin") != [self.policy.origin])
                    or any(
                        key.decode("latin1").lower().startswith(("cookie", "authorization", "forwarded", "x-forwarded"))
                        for key, _ in headers
                    )
                ):
                    raise RefreshRejected(403, "static_request_rejected")
                response = self._assets.response(path, head=scope["method"] == "HEAD")
                response.headers.update(
                    {
                        "Cache-Control": "no-store",
                        "Referrer-Policy": "same-origin",
                        "X-Content-Type-Options": "nosniff",
                        "Cross-Origin-Resource-Policy": "same-origin",
                    }
                )
                response.headers["Content-Security-Policy"] = response.headers["Content-Security-Policy"].replace(
                    "form-action 'self' https://owner-idp.invalid", "form-action 'none'"
                )
                await response(scope, receive, send)
                return
            if (
                scope.get("method") != "POST"
                or header("origin") != [self.policy.origin]
                or any(header(key) != [value] for key, value in PROFILE.items())
            ):
                raise RefreshRejected(403, "same_origin_required")
            if any(
                key.decode("latin1")
                .lower()
                .startswith(
                    (
                        "cookie",
                        "authorization",
                        "forwarded",
                        "x-forwarded",
                        "x-user",
                        "x-auth",
                        "x-identity",
                        "cf-",
                        "x-enrollment",
                        "x-csrf",
                    )
                )
                or (key.decode("latin1").lower().startswith("x-owner") and key != b"x-owner-client")
                for key, _ in headers
            ):
                raise RefreshRejected(400, "untrusted_header")
            if (
                scope.get("query_string")
                or scope.get("raw_path", path.encode()) != path.encode()
                or not path.startswith(BASE + "/")
            ):
                raise RefreshRejected(404, "not_found")
            if header("content-type") != ["application/json"] or header("transfer-encoding"):
                raise RefreshRejected(400, "unexpected_payload")
            raw = bytearray()
            async with asyncio.timeout(1):
                while True:
                    message = await receive()
                    if message["type"] != "http.request" or len(raw) + len(message.get("body", b"")) > 32768:
                        raise RefreshRejected(400, "unexpected_payload")
                    raw.extend(message.get("body", b""))
                    if not message.get("more_body"):
                        break
            payload = await self._dispatch_action(path[len(BASE) + 1 :], object_json(bytes(raw)))
            body = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode()
            self._return_current(payload)
            status = 200
        except RefreshRejected as exc:
            status, body = exc.status, json.dumps({"error": {"code": exc.code}}).encode()
        except Exception:
            status, body = 400, b'{"error":{"code":"owner_request_rejected"}}'
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
        if status == 200:
            try:
                self._return_current(payload)
            except Exception:
                body = b""  # headers may already be sent; fail closed without transmitting the protected body
        await send({"type": "http.response.body", "body": body})


def _token(value: Any) -> bool:
    import re

    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{43}", value) is not None


def create_genuine_owner_app(
    store: OwnerCredentialStore | None = None,
    *,
    enabled: bool = False,
    policy_accepted: bool = False,
    reads: LiveGenerations | None = None,
    live_reads_approved: bool = False,
    approved_evidence: LiveEvidenceBinding | None = None,
    browser_assets_root: Path | None = None,
) -> GenuineOwnerApp:
    """No store substitutes, synthetic state, optional durability or invented-IdP fallback."""
    if enabled is not True or policy_accepted is not True or type(store) is not OwnerCredentialStore:
        raise ValueError("genuine owner integration is disabled")
    if store.policy.purpose != GENUINE_PURPOSE or store.policy.origin != OWNER_ORIGIN:
        raise ValueError("genuine durable owner state required")
    if type(live_reads_approved) is not bool or (
        live_reads_approved
        and (
            type(reads) is not LiveGenerations
            or len(reads.allowlist.patients) != 1
            or type(approved_evidence) is not LiveEvidenceBinding
        )
    ):
        raise ValueError("one separately approved LIVE read source required")
    store.load()
    assets = BrowserAssets.load(browser_assets_root) if browser_assets_root is not None else None
    return GenuineOwnerApp(
        store,
        reads,
        live_reads_approved,
        assets,
        enabled=True,
        policy_accepted=True,
        approved_evidence=approved_evidence,
    )


def prepare_genuine_owner_https(app: GenuineOwnerApp, certificate: Path, key: Path, *, enabled: bool = False) -> Any:
    """Reuse the exact custody/SAN/SNI/actual-peer/context bootstrap. Prepare only; never bind or browse."""
    if enabled is not True or type(app) is not GenuineOwnerApp:
        raise ValueError("genuine HTTPS preparation is disabled")
    app._auth._owner_available()
    from api.beta_owner_https import prepare_bootstrap

    config = prepare_bootstrap(app.policy, certificate, key, enabled=True)
    config.app = app
    config.loaded_app = app
    return config
