"""Isolated local Beta origin. Only explicit in-process MOCK construction is supported.

An optional ``live`` argument adds a staging-only LIVE refresh for the aliases in its explicit, immutable
allowlist (empty approves nothing): refresh, status, revoke and candidate erasure recognise such an alias;
every other route stays MOCK-only. A LIVE attempt never publishes a generation. Its validated candidate is
kept privately for at most 24 hours (see ``beta_live_staging``) and is erased on revocation or owner request.
An additional constructor-only ``synthetic_live_records`` opt-in connects approved synthetic evidence to the
record GET and patient discovery contracts for offline verification only. It defaults off and refuses real
transports/evidence. The normal launcher never enables it.
"""

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

from api.beta_browser import (
    BROWSER_LOGIN,
    PATIENTS_ROUTE,
    SESSION_ROUTE,
    BrowserAssets,
    browser_path,
    form_profile,
    patient_view,
    payload_length,
    read_profile,
    session_view,
    static_namespace,
)
from api.beta_oidc import (
    FLOW_COOKIE,
    FLOW_TTL,
    HOST,
    ORIGIN,
    SESSION_ABSOLUTE,
    SESSION_COOKIE,
    INVENTED_IDENTITY,
    TOKEN_SHAPE,
    OwnerAuth,
    OwnerIdentity,
)
from api.beta_oidc_keys import JwksKeys, ProviderMetadata
from api.beta_record import record_view
from src.interop.epic import beta_catalog
from src.interop.epic.beta_acquisition import Acquisition, AcquisitionError, Limits, require_mock
from src.interop.epic.beta_catalog import MOCK_PATIENTS, Catalog, RefreshRejected
from src.interop.epic.beta_generations import Generations, Hook
from src.interop.epic.beta_live_acquisition import LiveAcquisition, LiveRefresh
from src.interop.epic.beta_live_generations import LiveGenerations
from src.interop.epic.beta_live_staging import LiveStaging
from src.interop.epic.beta_staging import encoded

LOGIN = ("POST", "/beta/auth/login")
CALLBACK = ("GET", "/beta/auth/callback")
STEP_UP = ("POST", "/beta/auth/step-up")
PATIENT_ROUTE = "/beta/epic/patients/{alias}"
IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9._:/-]{12,128}")
DEFAULT_LIMITS = Limits()
# Retention is time-driven, not request-driven: an idle process must not keep expired LIVE data. The pass is
# internal cleanup only (no network, no refresh, nothing exposed) and is read at each wait so tests can shorten it.
LIVE_PURGE_INTERVAL_S = 60.0
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
    staging: LiveStaging | None = None
    live_generations: LiveGenerations | None = None


def create_mock_beta_app(
    catalog_path: Path,
    *,
    oidc_key: rsa.RSAPublicKey | None,
    epic_key: rsa.RSAPrivateKey,
    oidc_transport: httpx.MockTransport,
    epic_transport: httpx.MockTransport,
    limits: Limits = DEFAULT_LIMITS,
    clock: Callable[[], float] = time.time,
    generation_root: Path | None = None,
    publication_hook: Hook | None = None,
    browser_assets_root: Path | None = None,
    live: LiveRefresh | None = None,
    identity: OwnerIdentity = INVENTED_IDENTITY,
    provider: ProviderMetadata | None = None,
    jwks: JwksKeys | None = None,
    synthetic_live_records: bool = False,
) -> FastAPI:
    """Build an isolated Beta origin with an invented owner and two invented patients."""
    require_mock(oidc_transport)
    require_mock(epic_transport)
    if type(synthetic_live_records) is not bool:
        raise ValueError("synthetic LIVE record opt-in must be a boolean")
    if synthetic_live_records and (
        live is None or not isinstance(live.transport, httpx.MockTransport) or browser_assets_root is None
    ):
        raise ValueError("synthetic LIVE record reads require an offline LIVE transport and browser assets")
    if min(oidc_key.key_size if oidc_key is not None else 2048, epic_key.key_size) < 2048:
        raise ValueError("MOCK signing keys must be at least 2048 bits")
    browser_assets = BrowserAssets.load(browser_assets_root) if browser_assets_root is not None else None
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
        if control.staging is not None:
            # After the cancel, so an in-flight LIVE attempt cannot stage a candidate behind the erase.
            control.staging.erase_all("owner_revoked")
        if control.live_generations is not None:
            control.live_generations.erase_all("owner_revoked")

    async def purge_loop() -> None:
        while True:
            await asyncio.sleep(LIVE_PURGE_INTERVAL_S)
            try:
                if control.staging is not None:
                    control.staging.purge()
                if control.live_generations is not None:
                    control.live_generations.purge()
            except Exception:
                try:
                    control.catalog.audit("live_purge_failed")
                except Exception:
                    pass  # the loop must outlive a failed pass; the next pass retries

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        control.catalog = Catalog(catalog_path.resolve(), clock)
        control.owner = OwnerAuth(control.catalog, oidc_key, oidc_transport, identity, provider=provider, keys=jwks)
        control.work = None
        control.generations = None
        control.staging = None
        control.live_generations = None
        app.state.revoke_owner = revoke_owner
        purger: asyncio.Task[None] | None = None
        try:
            if generation_root is not None:
                control.generations = Generations(control.catalog, generation_root, publication_hook)
            if live is not None:
                # Startup applies retention and revocation and removes anything the catalog does not own.
                control.staging = LiveStaging(control.catalog, catalog_path.resolve().parent / "live-staging")
                control.live_generations = LiveGenerations(
                    control.catalog,
                    catalog_path.resolve().parent / "live-generations",
                    live.allowlist,
                    control.staging,
                )
                purger = asyncio.create_task(purge_loop())
            yield
        finally:
            if purger is not None:
                purger.cancel()
                await asyncio.gather(purger, return_exceptions=True)
            if control.staging is not None:
                control.staging.purge()
            if control.live_generations is not None:
                control.live_generations.purge()
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
        path = request.scope["path"]
        browser_request = browser_assets is not None and browser_path(path)
        static_request = browser_assets is not None and static_namespace(path)
        try:
            route = (request.method, request.scope["path"])
            token = _cookie(request, SESSION_COOKIE)
            if request.url.scheme != "https" or _single_header(request, "host") != HOST:
                raise RefreshRejected(403, "invalid_origin")
            if any(name.lower().startswith(UNTRUSTED_HEADERS) for name in request.headers):
                raise RefreshRejected(400, "untrusted_header")
            if browser_request:
                if request.scope.get("raw_path") not in (None, path.encode("utf-8")):
                    raise RefreshRejected(404, "not_found")
                if static_request:
                    if request.method not in {"GET", "HEAD"}:
                        raise RefreshRejected(405, "method_not_allowed")
                    if request.scope["query_string"]:
                        raise RefreshRejected(400, "unexpected_payload")
                    payload_length(request)
                    await _require_empty(request)
                    assert browser_assets is not None
                    response = browser_assets.response(path, head=request.method == "HEAD")
                else:
                    session = control.owner.session(token)
                    if path != BROWSER_LOGIN and session is None:
                        raise RefreshRejected(401, "owner_required")
                    if request.method != ("POST" if path == BROWSER_LOGIN else "GET"):
                        raise RefreshRejected(405, "method_not_allowed")
                    if request.scope["query_string"]:
                        raise RefreshRejected(400, "unexpected_payload")
                    payload_length(request)
                    if path == BROWSER_LOGIN:
                        form_profile(request)
                    else:
                        read_profile(request)
                    await _require_empty(request)
                    if path != BROWSER_LOGIN and control.owner.session(token) is None:
                        raise RefreshRejected(401, "owner_required")
                    request.state.owner_session = token
                    response = await call_next(request)
                response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
            else:
                response = await legacy_boundary(request, route, token, call_next)
        except RefreshRejected as exc:
            if not static_request:
                control.catalog.audit(exc.code)
            response = _error(exc)
        except Exception:
            if not browser_request and not (browser_assets is not None and path == CALLBACK[1]):
                raise
            response = _error(RefreshRejected(500, "internal_error"))
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        if browser_request:
            response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
            if request.method == "HEAD":
                response = Response(status_code=response.status_code, headers=dict(response.headers))
        return response

    async def legacy_boundary(
        request: Request,
        route: tuple[str, str],
        token: str,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
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
        if route not in {LOGIN, CALLBACK} and control.owner.session(token) is None:
            raise RefreshRejected(401, "owner_required")
        request.state.owner_session = token
        return await call_next(request)

    @app.post(LOGIN[1])
    async def login(request: Request) -> Response:
        return begin_login(request, "legacy-json")

    def begin_login(request: Request, channel: str) -> Response:
        browser, url = control.owner.begin(_cookie(request, FLOW_COOKIE), channel=channel)
        control.catalog.audit("login_started")
        response = RedirectResponse(url, status_code=303)
        response.set_cookie(FLOW_COOKIE, browser, max_age=FLOW_TTL, secure=True, httponly=True, samesite="lax")
        return response

    if live is not None:

        @app.post(STEP_UP[1])
        async def step_up(request: Request) -> Response:
            """Reauthenticate the signed-in owner; the callback then grants one short-lived, one-shot elevation."""
            browser, url = control.owner.begin_step_up(request.state.owner_session, _cookie(request, FLOW_COOKIE))
            control.catalog.audit("step_up_started")
            response = RedirectResponse(url, status_code=303)
            response.set_cookie(FLOW_COOKIE, browser, max_age=FLOW_TTL, secure=True, httponly=True, samesite="lax")
            return response

    @app.get(CALLBACK[1])
    async def callback(request: Request) -> Response:
        if len(request.scope["query_string"]) > 2048 or len(request.query_params.multi_items()) != 2:
            raise RefreshRejected(401, "login_rejected")
        pending = control.owner.flows.get(_cookie(request, FLOW_COOKIE))
        if pending is not None and pending.purpose == "step-up":
            await control.owner.complete_step_up(
                _cookie(request, FLOW_COOKIE),
                request.query_params.get("state", ""),
                request.query_params.get("code", ""),
                request.state.owner_session,
            )
            control.catalog.audit("step_up_succeeded")
            granted = JSONResponse({"step_up": "granted", "expires_in": control.owner.identity.step_up_ttl_s})
            granted.delete_cookie(FLOW_COOKIE, secure=True, httponly=True, samesite="lax")
            return granted
        channel = await control.owner.complete(
            _cookie(request, FLOW_COOKIE),
            request.query_params.get("state", ""),
            request.query_params.get("code", ""),
        )
        if channel == "browser-shell" and browser_assets is None:
            raise RefreshRejected(401, "login_rejected")
        previous = request.state.owner_session
        token, session = control.owner.create_session(previous)
        if control.work is not None and control.work.session == previous:
            await cancel(control.work, "authorization_revoked")
        if control.owner.session(token) is None:
            raise RefreshRejected(401, "login_rejected")
        control.catalog.audit("login_succeeded")
        response = (
            RedirectResponse("/beta/app/", status_code=303)
            if channel == "browser-shell"
            else JSONResponse({"csrf_token": session.csrf})
        )
        response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_ABSOLUTE, secure=True, httponly=True, samesite="lax")
        response.delete_cookie(FLOW_COOKIE, secure=True, httponly=True, samesite="lax")
        return response

    if browser_assets is not None:

        @app.post(BROWSER_LOGIN)
        async def browser_login(request: Request) -> Response:
            return begin_login(request, "browser-shell")

        @app.get(SESSION_ROUTE)
        async def browser_session(request: Request) -> JSONResponse:
            session = control.owner.session(request.state.owner_session)
            if session is None:
                raise RefreshRejected(401, "owner_required")
            return JSONResponse(session_view(session, control.catalog.clock()))

        @app.get(PATIENTS_ROUTE)
        async def browser_patients(request: Request) -> JSONResponse:
            if control.owner.session(request.state.owner_session) is None:
                raise RefreshRejected(401, "owner_required")
            result = patient_view(control.catalog)
            if synthetic_live_records and live is not None:
                # Discovery passes the SAME synthetic marker, integrity and return-time authorization fences
                # as a record read. Never advertise an allowlisted but unapproved/real/expired generation.
                result.update(mode="SYNTHETIC", source="offline-synthetic-preview")
                for alias in live.allowlist.patients:
                    try:
                        view = synthetic_record(request, alias)
                    except RefreshRejected as exc:
                        if exc.status == 401:
                            raise
                        continue
                    result["patients"].append(
                        {"alias": alias, "label": view["label"], "mode": "LIVE", "data_origin": view["data_origin"]}
                    )
            if control.owner.session(request.state.owner_session) is None:
                raise RefreshRejected(401, "owner_required")
            return JSONResponse(result)

        def synthetic_record(request: Request, alias: str) -> dict:
            from api.beta_live_record import live_record_view

            resolve(request, alias)
            if control.live_generations is None:
                raise RefreshRejected(404, "patient_unavailable")
            token = request.state.owner_session

            def authorized() -> bool:
                return control.owner.session(token) is not None and not control.catalog.revoked(alias)

            return live_record_view(control.live_generations, alias, authorized=authorized, synthetic_only=True)

        @app.get(PATIENTS_ROUTE + "/{alias}/record")
        async def browser_record(request: Request, alias: str) -> JSONResponse:
            """Passive owner read of one MOCK patient's active generation, or an explicitly enabled synthetic preview.

            LIVE aliases stay 404 by default. The offline opt-in cannot read real evidence or start acquisition."""
            if control.owner.session(request.state.owner_session) is None:
                raise RefreshRejected(401, "owner_required")
            if synthetic_live_records and live is not None and live.allowlist.get(alias) is not None:
                # Constructor-only, synthetic-only seam; no environment/header/query switch or launcher enables it.
                return JSONResponse(synthetic_record(request, alias))
            if alias not in MOCK_PATIENTS or control.catalog.revoked(alias):
                raise RefreshRejected(404, "patient_unavailable")
            generations = control.generations
            state = generations.status(alias) if generations is not None else control.catalog.status(alias)
            runtime = manifest = None
            if generations is not None and state.get("active") and "evidence_error" not in state:
                runtime, manifest = generations.runtime(alias, state["active"]["generation_id"])
            return JSONResponse(record_view(alias, state, runtime, manifest))

    def touch(request: Request) -> None:
        if control.owner.session(request.state.owner_session, touch=True) is None:
            raise RefreshRejected(401, "owner_required")

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

    def resolve(request: Request, alias: str) -> tuple[str, bool]:
        """Patient id and whether it is a LIVE alias. Only refresh, status, revoke and erase use this."""
        if control.owner.session(request.state.owner_session) is None:
            raise RefreshRejected(401, "owner_required")
        if control.staging is not None:
            control.staging.purge()  # retention and revocation are applied on every LIVE-capable request
        if control.live_generations is not None:
            control.live_generations.purge()
        patient = MOCK_PATIENTS.get(alias)
        is_live = False
        if patient is None and live is not None:
            patient = live.allowlist.get(alias)
            is_live = patient is not None
        if patient is None or control.catalog.revoked(alias):
            raise RefreshRejected(404, "patient_unavailable")
        return patient, is_live

    @app.get(PATIENT_ROUTE + "/status")
    async def status(request: Request, alias: str) -> JSONResponse:
        _, is_live = resolve(request, alias)
        if is_live:
            assert control.staging is not None and control.live_generations is not None
            return JSONResponse(
                {
                    **control.catalog.status(alias, live=True),
                    "candidate": control.staging.view(alias),
                    "generation": control.live_generations.view(alias),
                }
            )
        return JSONResponse(
            control.generations.status(alias) if control.generations is not None else control.catalog.status(alias)
        )

    @app.post(PATIENT_ROUTE + "/revoke")
    async def revoke_patient(request: Request, alias: str) -> Response:
        _, is_live = resolve(request, alias)
        control.catalog.revoke(alias)
        if control.work is not None and control.work.alias == alias:
            await cancel(control.work, "authorization_revoked")
        if is_live and control.staging is not None:
            # After the cancel, so an in-flight LIVE attempt cannot stage a candidate behind the erase.
            control.staging.erase_alias(alias, "revoked")
            if control.live_generations is not None:
                control.live_generations.erase_alias(alias, "revoked")
        return Response(status_code=204)

    @app.post(PATIENT_ROUTE + "/candidate/erase")
    async def erase_candidate(request: Request, alias: str) -> Response:
        """Owner-requested erasure of a LIVE alias's staged candidate; also stops an in-flight LIVE attempt."""
        _, is_live = resolve(request, alias)
        if not is_live or control.staging is None:
            raise RefreshRejected(404, "patient_unavailable")
        if control.work is not None and control.work.alias == alias:
            await cancel(control.work, "owner_erase")
        control.staging.erase_alias(alias, "owner_requested")
        return Response(status_code=204)

    @app.post(PATIENT_ROUTE + "/generation/erase")
    async def erase_generation(request: Request, alias: str) -> Response:
        """Owner-requested erasure of a LIVE alias's approved generation."""
        _, is_live = resolve(request, alias)
        if not is_live or control.live_generations is None:
            raise RefreshRejected(404, "patient_unavailable")
        control.live_generations.erase_alias(alias, "owner_requested")
        return Response(status_code=204)

    @app.post(PATIENT_ROUTE + "/candidate/{attempt}/approve")
    async def approve_candidate(request: Request, alias: str, attempt: str) -> JSONResponse:
        """Data-governance approval of one staged candidate, bound to its attempt id and manifest hash.

        Not a clinical review. The recent-login check is a local/offline safeguard, not reauthentication; real
        LIVE data cannot be promoted until genuine owner authentication exists (``real_promotion`` is False).
        """
        _, is_live = resolve(request, alias)
        if not is_live or control.live_generations is None:
            raise RefreshRejected(404, "patient_unavailable")
        if (
            control.work is not None
            and control.work.alias == alias
            and not (control.work.task is None or control.work.task.done())
        ):
            raise RefreshRejected(409, "refresh_busy")
        token = request.state.owner_session

        def authorized() -> bool:
            return control.owner.session(token) is not None and not control.catalog.revoked(alias)

        csrf = _single_header(request, "x-csrf-token")

        def fresh() -> bool:
            # One-shot step-up elevation bound to this session and CSRF token; consumed here, used or not.
            return control.owner.consume_elevation(token, csrf)

        result = control.live_generations.approve(
            alias, attempt, _single_header(request, "x-beta-candidate-hash"), authorized, fresh
        )
        touch(request)
        return JSONResponse(result, status_code=201)

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

    async def acquire_live(item: Work) -> None:
        """Staging only: run the LIVE adapter, record content-free counts and codes, stage the candidate privately.

        The validated candidate is written to private staging (24-hour maximum, erased on revocation or owner
        request) and nothing else: no generation, no publication, so nothing can be served, selected or cited.
        """
        assert live is not None and control.staging is not None
        staging = control.staging

        def authorized() -> bool:
            return control.owner.session(item.session) is not None and not control.catalog.revoked(item.alias)

        adapter: LiveAcquisition | None = None

        def operations() -> dict[str, dict[str, object]] | None:
            return adapter.report() if adapter is not None else None

        try:
            control.catalog.start(item.attempt)
            adapter = LiveAcquisition(
                control.catalog,
                item.attempt,
                item.alias,
                live.key,
                live.config,
                allowlist=live.allowlist,
                enabled=True,
                authorized=authorized,
                limits=limits,
                transport=live.transport,
            )
            await adapter.run()
            if adapter.candidate is None:
                raise AcquisitionError("internal_error")
            manifest, bundle = adapter.candidate
            # No await from here to the end of staging: a revocation cannot interleave with the write.
            staging.stage(item.attempt, item.alias, encoded(manifest), bundle, authorized)
            adapter.candidate = None
        except asyncio.CancelledError:
            control.catalog.finish(item.attempt, "interrupted", item.cancel_code, operations())
        except (AcquisitionError, RefreshRejected) as exc:
            state = "interrupted" if exc.code == "authorization_revoked" else "failed"
            control.catalog.finish(item.attempt, state, exc.code, operations())
        except Exception:
            control.catalog.finish(item.attempt, "failed", "internal_error", operations())
        else:
            control.catalog.finish(item.attempt, "succeeded", operations=operations())

    @app.post(PATIENT_ROUTE + "/refresh")
    async def refresh(request: Request, alias: str) -> JSONResponse:
        patient, is_live = resolve(request, alias)
        key = _single_header(request, "idempotency-key")
        if not IDEMPOTENCY_KEY.fullmatch(key):
            raise RefreshRejected(400, "idempotency_key_required")
        if not is_live and control.generations is not None and control.catalog.existing_attempt(alias, key) is None:
            control.generations.quota()
        deadline = time.monotonic() + limits.deadline_s
        attempt, created = control.catalog.admit(
            alias,
            key,
            cooldown_s=limits.cooldown_s,
            patient_limit=limits.patient_per_hour,
            global_limit=limits.global_per_hour,
            request_limit=limits.global_requests_per_hour,
            policy=beta_catalog.LIVE_POLICY_VERSION if is_live else beta_catalog.POLICY_VERSION,
        )
        if created:
            touch(request)
            control.work = Work(attempt, alias, request.state.owner_session)
            control.work.task = asyncio.create_task(
                acquire_live(control.work) if is_live else acquire(control.work, patient, deadline)
            )
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
        result = store.select(alias, gid, _single_header(request, "x-beta-cache-ack") == "1", explicit=True)
        touch(request)
        return JSONResponse(result)

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
        patient_id(request, alias)
        touch(request)
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
        touch(request)
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
        store = generation_store(request, alias)
        acknowledgement = _single_header(request, "x-beta-cache-ack") == "1"
        # Preserve the original clinical-use boundary before inspecting a persisted run.
        store.select(alias, gid, acknowledgement)
        runtime, _ = store.pinned(alias, gid, identifier, "run")
        existing = runtime.db.one("SELECT review_id FROM review WHERE artifact_id = ? LIMIT 1", (identifier,))
        result = store.submit_review(
            alias,
            gid,
            identifier,
            acknowledgement,
        )
        if existing is None:
            touch(request)
        return JSONResponse(result)

    @app.get(generation_route + "/reviews/{identifier}")
    async def get_review(request: Request, alias: str, gid: str, identifier: str) -> JSONResponse:
        _, result = generation_store(request, alias).pinned(alias, gid, identifier, "review")
        return JSONResponse(result)

    @app.post(generation_route + "/reviews/{identifier}/decide")
    async def decide_review(request: Request, alias: str, gid: str, identifier: str) -> JSONResponse:
        generation_store(request, alias).pinned(alias, gid, identifier, "review")
        raise RefreshRejected(403, "independent_reviewer_required")

    return app
