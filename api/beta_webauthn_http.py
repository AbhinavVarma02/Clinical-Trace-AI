"""Explicitly enabled auth-only ASGI seam for offline local-owner WebAuthn verification.

Never mounted in Beta, Stable or the MOCK launcher. No listener, TLS bypass, credential file, clinical route or
enrollment-grant issuer. Genuine HTTPS trust, loopback serving and real enrollment are separate acceptance gates.
"""

from __future__ import annotations

import asyncio
import ipaddress
import secrets
from typing import Any

from fastapi import FastAPI, Request
from starlette.responses import JSONResponse, Response

from api.beta_oidc import SESSION_ABSOLUTE, TOKEN_SHAPE
from api.beta_webauthn import CEREMONY_S, LOCAL_BIND, LocalOwnerAuth
from api.beta_webauthn_store import PURPOSE
from src.interop.epic.beta_acquisition import object_json
from src.interop.epic.beta_catalog import RefreshRejected

BASE = "/owner/auth"
FLOW_COOKIE = "__Host-owner-webauthn-flow"
PROFILE = {
    "x-owner-client": "webauthn-v1",
    "sec-fetch-site": "same-origin",
    "sec-fetch-mode": "same-origin",
    "sec-fetch-dest": "empty",
    "accept": "application/json",
}
PUBLIC = {"register/options", "register/finish", "login/options", "login/finish"}
ACTIONS = PUBLIC | {"step-up/options", "step-up/finish", "logout", "revoke"}


def _header(request: Request, name: str) -> str:
    values = request.headers.getlist(name)
    return values[0] if len(values) == 1 else ""


def _cookie(request: Request, name: str) -> str:
    headers = request.headers.getlist("cookie")
    if len(headers) > 1:
        raise RefreshRejected(400, "invalid_cookie")
    values = [
        part.strip().split("=", 1)[1]
        for part in (headers[0].split(";") if headers else [])
        if part.strip().startswith(name + "=")
    ]
    if len(values) > 1 or (values and not TOKEN_SHAPE.fullmatch(values[0])):
        raise RefreshRejected(400, "invalid_cookie")
    return values[0] if values else ""


def create_local_owner_auth_app(owner: LocalOwnerAuth, *, enabled: bool = False) -> FastAPI:
    if enabled is not True or owner.policy.purpose != PURPOSE:
        raise ValueError("local-owner auth seam is disabled by default")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)
    app.state.bind_host = LOCAL_BIND  # future reviewed launcher must use this fixed address; no listener is created

    @app.middleware("http")
    async def boundary(request: Request, call_next):
        try:
            try:
                local = request.client is not None and ipaddress.ip_address(request.client.host).is_loopback
            except ValueError:
                local = False
            if not local or request.url.scheme != "https" or _header(request, "host") != owner.policy.authority:
                raise RefreshRejected(403, "local_https_required")
            if any(name.lower().startswith("x-owner") and name.lower() != "x-owner-client" for name in request.headers):
                raise RefreshRejected(400, "untrusted_header")
            if any(
                name.lower().startswith(
                    (
                        "authorization",
                        "forwarded",
                        "x-forwarded",
                        "x-user",
                        "x-identity",
                        "x-auth",
                        "x-data-source",
                        "x-api-key",
                        "cf-",
                    )
                )
                for name in request.headers
            ):
                raise RefreshRejected(400, "untrusted_header")
            if any(_header(request, name) != value for name, value in PROFILE.items()):
                raise RefreshRejected(403, "same_origin_required")
            origin = request.headers.getlist("origin")
            if (request.method == "POST" and origin != [owner.policy.origin]) or (
                origin and origin != [owner.policy.origin]
            ):
                raise RefreshRejected(403, "invalid_origin")
            path = request.scope["path"]
            if request.scope.get("raw_path") not in (None, path.encode()) or request.scope["query_string"]:
                raise RefreshRejected(400, "unexpected_payload")
            action = path.removeprefix(BASE + "/")
            if path != BASE + "/" + action or action not in ACTIONS | {"session"}:
                raise RefreshRejected(404, "not_found")
            if request.method != ("GET" if action == "session" else "POST"):
                raise RefreshRejected(405, "method_not_allowed")
            token, csrf = _cookie(request, owner.policy.session_cookie), _header(request, "x-csrf-token")
            if action not in PUBLIC:
                if action == "session":
                    if owner.session(token) is None:
                        raise RefreshRejected(401, "owner_required")
                else:
                    owner.require_session(token, csrf)
            raw = bytearray()
            try:
                async with asyncio.timeout(1):
                    async for chunk in request.stream():
                        if len(raw) + len(chunk) > 32 * 1024:
                            raise RefreshRejected(400, "unexpected_payload")
                        raw.extend(chunk)
            except TimeoutError:
                raise RefreshRejected(400, "unexpected_payload") from None
            if action == "session":
                if raw or owner.session(token) is None:
                    raise RefreshRejected(401, "owner_required")
                request.state.body = {}
            else:
                if _header(request, "content-type") != "application/json":
                    raise RefreshRejected(400, "unexpected_payload")
                try:
                    request.state.body = object_json(bytes(raw))
                except Exception:
                    raise RefreshRejected(400, "unexpected_payload") from None
                if action not in PUBLIC:
                    owner.require_session(token, csrf)  # recheck after awaiting the request body
            request.state.token, request.state.csrf = token, csrf
            response = await call_next(request)
        except RefreshRejected as exc:
            response = JSONResponse({"error": {"code": exc.code}}, status_code=exc.status)
        except Exception:
            response = JSONResponse({"error": {"code": "authentication_error"}}, status_code=400)
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
                "Cross-Origin-Resource-Policy": "same-origin",
            }
        )
        return response

    @app.exception_handler(RefreshRejected)
    async def rejected(_: Request, error: RefreshRejected):
        return JSONResponse({"error": {"code": error.code}}, status_code=error.status)

    @app.get(BASE + "/session")
    async def session_view(request: Request):
        session = owner.session(request.state.token)
        if session is None:
            raise RefreshRejected(401, "owner_required")
        return JSONResponse(
            {
                "authenticated": True,
                "method": "webauthn",
                "csrf_token": session.csrf,
                "server_time_unix_s": owner.clock(),
                "idle_expires_at_unix_s": session.used + 900,
                "absolute_expires_at_unix_s": session.created + SESSION_ABSOLUTE,
            }
        )

    @app.post(BASE + "/{action:path}")
    async def ceremony(request: Request, action: str):
        body: dict[str, Any] = request.state.body
        token, csrf = request.state.token, request.state.csrf
        response: Response
        if action.endswith("/options"):
            if body:
                raise RefreshRejected(400, "unexpected_payload")
            browser = _cookie(request, FLOW_COOKIE) or secrets.token_urlsafe(32)
            if action == "register/options":
                options = owner.begin_registration(_header(request, "x-enrollment-grant"), browser)
            elif action == "login/options":
                options = owner.begin_login(browser)
            else:
                options = owner.begin_step_up(token, csrf, browser)
            response = JSONResponse(options)
            response.set_cookie(FLOW_COOKIE, browser, secure=True, httponly=True, samesite="strict", max_age=CEREMONY_S)
            return response
        if action.endswith("/finish"):
            if (
                set(body) != {"flow_id", "credential"}
                or not isinstance(body["flow_id"], str)
                or not isinstance(body["credential"], dict)
            ):
                raise RefreshRejected(400, "unexpected_payload")
            browser = _cookie(request, FLOW_COOKIE)
            if action == "register/finish":
                owner.complete_registration(body["flow_id"], browser, body["credential"])
                return JSONResponse({"enrolled": True})
            if action == "step-up/finish":
                owner.complete_step_up(body["flow_id"], browser, body["credential"], token, csrf)
                return JSONResponse({"step_up": "granted", "expires_in": 120})
            token, session = owner.complete_login(body["flow_id"], browser, body["credential"])
            response = JSONResponse({"authenticated": True, "csrf_token": session.csrf, "method": "webauthn"})
            response.set_cookie(
                owner.policy.session_cookie,
                token,
                secure=True,
                httponly=True,
                samesite="strict",
                max_age=SESSION_ABSOLUTE,
            )
            response.delete_cookie(FLOW_COOKIE, secure=True, httponly=True, samesite="strict")
            return response
        if body:
            raise RefreshRejected(400, "unexpected_payload")
        if action == "revoke":
            if not owner.consume_elevation(token, csrf):
                raise RefreshRejected(401, "step_up_required")
            owner.revoke_owner()
        else:
            owner.logout(token, csrf)
        response = Response(status_code=204)
        for name in (owner.policy.session_cookie, FLOW_COOKIE):
            response.delete_cookie(name, secure=True, httponly=True, samesite="strict")
        return response

    return app
