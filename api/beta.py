"""Isolated local Beta origin. Only explicit in-process MOCK construction is supported."""

from __future__ import annotations

import asyncio
import re
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable

import httpx
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Request
from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse, RedirectResponse, Response

from api.beta_oidc import (
    FLOW_COOKIE,
    FLOW_TTL,
    HOST,
    ORIGIN,
    SESSION_ABSOLUTE,
    SESSION_COOKIE,
    TOKEN_SHAPE,
    OwnerAuth,
)
from src.interop.epic.beta_acquisition import Acquisition, AcquisitionError, Limits, require_mock
from src.interop.epic.beta_catalog import MOCK_PATIENTS, Catalog, RefreshRejected
from src.interop.epic.beta_generations import Generations, Hook

LOGIN = ("POST", "/beta/auth/login")
CALLBACK = ("GET", "/beta/auth/callback")
PATIENT_ROUTE = "/beta/epic/patients/{alias}"
IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9._:/-]{12,128}")
DEFAULT_LIMITS = Limits()
UNTRUSTED_HEADERS = (
    "x-owner",
    "x-user",
    "x-identity",
    "x-auth",
    "x-remote-user",
    "remote-user",
    "x-demo-",
    "x-data-source",
    "x-gateway",
    "x-forwarded",
    "x-original-",
    "x-rewrite-",
    "forwarded",
    "cf-",
    "true-client-ip",
    "x-edge-",
    "x-api-key",
    "authorization",
)


def _single_header(request: Request, name: str) -> str:
    values = request.headers.getlist(name)
    return values[0] if len(values) == 1 else ""


def _cookie(request: Request, name: str) -> str:
    # Reject ambiguous duplicate cookies rather than depending on parser ordering.
    values = [
        value.strip()
        for header in request.headers.getlist("cookie")
        for pair in header.split(";")
        for key, separator, value in (pair.strip().partition("="),)
        if key == name and separator
    ]
    if len(values) == 1 and TOKEN_SHAPE.fullmatch(values[0]):
        return values[0]
    return ""


def _error(exc: RefreshRejected) -> JSONResponse:
    headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after is not None else None
    return JSONResponse({"error": {"code": exc.code}}, status_code=exc.status, headers=headers)


async def _require_empty(request: Request) -> None:
    try:
        async with asyncio.timeout(1):
            async for chunk in request.stream():
                if chunk:
                    raise RefreshRejected(400, "unexpected_payload")
    except (TimeoutError, ClientDisconnect):
        raise RefreshRejected(400, "unexpected_payload") from None


@dataclass
class Work:
    attempt: str
    alias: str
    session: str = field(repr=False)
    task: asyncio.Task[None] | None = None
    cancel_code: str = "shutdown"


class Control:
    catalog: Catalog
    owner: OwnerAuth
    work: Work | None = None
    generations: Generations | None = None


def create_mock_beta_app(
    catalog_path: Path,
    *,
    oidc_key: rsa.RSAPublicKey,
    epic_key: rsa.RSAPrivateKey,
    oidc_transport: httpx.MockTransport,
    epic_transport: httpx.MockTransport,
    limits: Limits = DEFAULT_LIMITS,
    clock: Callable[[], float] = time.time,
    generation_root: Path | None = None,
    publication_hook: Hook | None = None,
) -> FastAPI:
    """Build an isolated Beta origin with an invented owner and two invented patients."""
    require_mock(oidc_transport)
    require_mock(epic_transport)
    if min(oidc_key.key_size, epic_key.key_size) < 2048:
        raise ValueError("MOCK signing keys must be at least 2048 bits")
    control = Control()

    async def cancel(item: Work, code: str) -> None:
        if item.task is None or item.task.done():
            return
        item.cancel_code = code
        item.task.cancel()
        await asyncio.gather(item.task, return_exceptions=True)
        # A task cancelled before its first instruction never runs its handler.
        control.catalog.finish(item.attempt, "interrupted", item.cancel_code)

    async def revoke_owner() -> None:
        control.catalog.revoke("owner")
        control.owner.sessions.clear()
        if control.work is not None:
            await cancel(control.work, "authorization_revoked")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        control.catalog = Catalog(catalog_path.resolve(), clock)
        control.owner = OwnerAuth(control.catalog, oidc_key, oidc_transport)
        control.work = None
        control.generations = None
        app.state.revoke_owner = revoke_owner
        try:
            if generation_root is not None:
                control.generations = Generations(control.catalog, generation_root, publication_hook)
            yield
        finally:
            if control.work is not None:
                await cancel(control.work, "shutdown")
            if control.generations is not None:
                control.generations.close()
            control.owner.sessions.clear()
            control.owner.flows.clear()
            control.catalog.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)

    @app.exception_handler(RefreshRejected)
    async def rejected(_: Request, exc: RefreshRejected) -> JSONResponse:
        control.catalog.audit(exc.code)
        return _error(exc)

    @app.middleware("http")
    async def owner_boundary(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        try:
            route = (request.method, request.scope["path"])
            token = _cookie(request, SESSION_COOKIE)
            if request.url.scheme != "https" or _single_header(request, "host") != HOST:
                raise RefreshRejected(403, "invalid_origin")
            if any(name.lower().startswith(UNTRUSTED_HEADERS) for name in request.headers):
                raise RefreshRejected(400, "untrusted_header")
            session = control.owner.session(token)
            if route not in {LOGIN, CALLBACK} and session is None:
                raise RefreshRejected(401, "owner_required")
            if request.scope.get("raw_path") not in (None, route[1].encode("utf-8")):
                raise RefreshRejected(404, "not_found")
            if route != CALLBACK and request.scope["query_string"]:
                raise RefreshRejected(400, "unexpected_payload")
            if request.method not in {"GET", "HEAD"}:
                if _single_header(request, "origin") != ORIGIN:
                    raise RefreshRejected(403, "invalid_origin")
                csrf = _single_header(request, "x-csrf-token")
                if route != LOGIN and (
                    session is None or not TOKEN_SHAPE.fullmatch(csrf) or not secrets.compare_digest(csrf, session.csrf)
                ):
                    raise RefreshRejected(403, "csrf_rejected")
                if not (control.generations is not None and route[0] == "POST" and route[1].endswith("/runs")):
                    await _require_empty(request)
            if route not in {LOGIN, CALLBACK} and control.owner.session(token, touch=True) is None:
                raise RefreshRejected(401, "owner_required")
            request.state.owner_session = token
            response = await call_next(request)
        except RefreshRejected as exc:
            control.catalog.audit(exc.code)
            response = _error(exc)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.post(LOGIN[1])
    async def login(request: Request) -> Response:
        browser, url = control.owner.begin(_cookie(request, FLOW_COOKIE))
        control.catalog.audit("login_started")
        response = RedirectResponse(url, status_code=303)
        response.set_cookie(FLOW_COOKIE, browser, max_age=FLOW_TTL, secure=True, httponly=True, samesite="lax")
        return response

    @app.get(CALLBACK[1])
    async def callback(request: Request) -> Response:
        if len(request.scope["query_string"]) > 2048 or len(request.query_params.multi_items()) != 2:
            raise RefreshRejected(401, "login_rejected")
        await control.owner.complete(
            _cookie(request, FLOW_COOKIE),
            request.query_params.get("state", ""),
            request.query_params.get("code", ""),
        )
        previous = request.state.owner_session
        token, session = control.owner.create_session(previous)
        if control.work is not None and control.work.session == previous:
            await cancel(control.work, "authorization_revoked")
        control.catalog.audit("login_succeeded")
        response = JSONResponse({"csrf_token": session.csrf})
        response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_ABSOLUTE, secure=True, httponly=True, samesite="lax")
        response.delete_cookie(FLOW_COOKIE, secure=True, httponly=True, samesite="lax")
        return response

    @app.post("/beta/auth/logout")
    async def logout(request: Request) -> Response:
        token = request.state.owner_session
        control.owner.sessions.pop(token, None)
        if control.work is not None and control.work.session == token:
            await cancel(control.work, "authorization_revoked")
        control.catalog.audit("session_revoked")
        response = Response(status_code=204)
        response.delete_cookie(SESSION_COOKIE, secure=True, httponly=True, samesite="lax")
        return response

    def patient_id(request: Request, alias: str) -> str:
        if control.owner.session(request.state.owner_session) is None:
            raise RefreshRejected(401, "owner_required")
        patient = MOCK_PATIENTS.get(alias)
        if patient is None or control.catalog.revoked(alias):
            raise RefreshRejected(404, "patient_unavailable")
        return patient

    @app.get(PATIENT_ROUTE + "/status")
    async def status(request: Request, alias: str) -> JSONResponse:
        patient_id(request, alias)
        return JSONResponse(
            control.generations.status(alias) if control.generations is not None else control.catalog.status(alias)
        )

    @app.post(PATIENT_ROUTE + "/revoke")
    async def revoke_patient(request: Request, alias: str) -> Response:
        patient_id(request, alias)
        control.catalog.revoke(alias)
        if control.work is not None and control.work.alias == alias:
            await cancel(control.work, "authorization_revoked")
        return Response(status_code=204)

    async def acquire(item: Work, patient: str, deadline: float) -> None:
        def authorized() -> bool:
            return control.owner.session(item.session) is not None and not control.catalog.revoked(item.alias)

        attempt = Acquisition(
            control.catalog,
            item.attempt,
            patient,
            epic_key,
            epic_transport,
            limits,
            deadline,
            authorized,
            retain=control.generations is not None,
        )
        try:
            control.catalog.start(item.attempt)
            await attempt.run()
            if control.generations is not None:
                await control.generations.publish(attempt, item.alias)
        except asyncio.CancelledError:
            control.catalog.finish(item.attempt, "interrupted", item.cancel_code, attempt.operations)
        except (AcquisitionError, RefreshRejected) as exc:
            state = "interrupted" if exc.code == "authorization_revoked" else "failed"
            control.catalog.finish(item.attempt, state, exc.code, attempt.operations)
        except Exception:
            # A transport or fixture can raise arbitrary exceptions with upstream content.
            control.catalog.finish(item.attempt, "failed", "internal_error", attempt.operations)
        else:
            control.catalog.finish(item.attempt, "succeeded", operations=attempt.operations)

    @app.post(PATIENT_ROUTE + "/refresh")
    async def refresh(request: Request, alias: str) -> JSONResponse:
        patient = patient_id(request, alias)
        key = _single_header(request, "idempotency-key")
        if not IDEMPOTENCY_KEY.fullmatch(key):
            raise RefreshRejected(400, "idempotency_key_required")
        if control.generations is not None and control.catalog.existing_attempt(alias, key) is None:
            control.generations.quota()
        deadline = time.monotonic() + limits.deadline_s
        attempt, created = control.catalog.admit(
            alias,
            key,
            cooldown_s=limits.cooldown_s,
            patient_limit=limits.patient_per_hour,
            global_limit=limits.global_per_hour,
            request_limit=limits.global_requests_per_hour,
        )
        if created:
            control.work = Work(attempt, alias, request.state.owner_session)
            control.work.task = asyncio.create_task(acquire(control.work, patient, deadline))
        return JSONResponse({"attempt_id": attempt}, status_code=202)

    def generation_store(request: Request, alias: str) -> Generations:
        patient_id(request, alias)
        if control.generations is None:
            raise RefreshRejected(404, "not_found")
        return control.generations

    generation_route = PATIENT_ROUTE + "/generations/{gid}"

    @app.post(generation_route + "/select")
    async def select_generation(request: Request, alias: str, gid: str) -> JSONResponse:
        store = generation_store(request, alias)
        return JSONResponse(store.select(alias, gid, _single_header(request, "x-beta-cache-ack") == "1", explicit=True))

    @app.post(generation_route + "/rollback")
    async def rollback(request: Request, alias: str, gid: str) -> JSONResponse:
        store = generation_store(request, alias)
        session = control.owner.session(request.state.owner_session)
        if session is None or control.catalog.clock() - session.created >= 300:
            raise RefreshRejected(401, "fresh_login_required")
        result = await store.rollback(
            alias,
            gid,
            lambda: control.owner.session(request.state.owner_session) is not None
            and not control.catalog.revoked(alias),
        )
        return JSONResponse(result)

    @app.post(generation_route + "/runs")
    async def run_generation(request: Request, alias: str, gid: str) -> JSONResponse:
        store = generation_store(request, alias)
        try:
            async with asyncio.timeout(1):
                body = bytearray()
                async for chunk in request.stream():
                    if len(body) + len(chunk) > 4096:
                        raise RefreshRejected(400, "invalid_question")
                    body.extend(chunk)
            from src.interop.epic.beta_acquisition import object_json

            value = object_json(bytes(body))
            question = value.get("question")
            if set(value) != {"question"} or not isinstance(question, str) or not 1 <= len(question) <= 1000:
                raise RefreshRejected(400, "invalid_question")
        except (TimeoutError, AcquisitionError, ClientDisconnect):
            raise RefreshRejected(400, "invalid_question") from None
        from src.interop.epic.mapping import internal_id
        from src.safety.guards import classify_question

        other_patients = tuple(
            marker
            for other, patient in MOCK_PATIENTS.items()
            if other != alias
            for marker in (other, patient, internal_id("Patient", patient))
        )
        if (
            classify_question(question, internal_id("Patient", MOCK_PATIENTS[alias]), other_patients).category
            == "cross_patient"
        ):
            raise RefreshRejected(403, "patient_scope")
        patient_id(request, alias)
        runtime, manifest, identifier = store.pin_run(alias, gid, _single_header(request, "x-beta-cache-ack") == "1")
        try:
            result = await asyncio.to_thread(store.execute, runtime, manifest, identifier, question)
        except Exception:
            raise RefreshRejected(409, "clinical_run_failed") from None
        patient_id(request, alias)
        store.runtime(alias, gid)
        store.finish_run(identifier, result)
        return JSONResponse(store.present(alias, gid, result))

    @app.get(generation_route + "/runs/{identifier}")
    async def get_run(request: Request, alias: str, gid: str, identifier: str) -> JSONResponse:
        store = generation_store(request, alias)
        _, result = store.pinned(alias, gid, identifier, "run")
        return JSONResponse(store.present(alias, gid, result))

    @app.post(generation_route + "/runs/{identifier}/review")
    async def submit_review(request: Request, alias: str, gid: str, identifier: str) -> JSONResponse:
        return JSONResponse(
            generation_store(request, alias).submit_review(
                alias,
                gid,
                identifier,
                _single_header(request, "x-beta-cache-ack") == "1",
            )
        )

    @app.get(generation_route + "/reviews/{identifier}")
    async def get_review(request: Request, alias: str, gid: str, identifier: str) -> JSONResponse:
        _, result = generation_store(request, alias).pinned(alias, gid, identifier, "review")
        return JSONResponse(result)

    @app.post(generation_route + "/reviews/{identifier}/decide")
    async def decide_review(request: Request, alias: str, gid: str, identifier: str) -> JSONResponse:
        generation_store(request, alias).pinned(alias, gid, identifier, "review")
        raise RefreshRejected(403, "independent_reviewer_required")

    return app
