"""FastAPI application entry point for Clinical-Trace AI.

Builds the FastAPI app and mounts:

* the original v1 prediction/explanation routes (``api.routes``) - unchanged;
* the v2 clinical workspace API (``api.v2``) and the read-only mock FHIR R4
  server (``api.fhir``) over synthetic records;
* the compiled React workspace at ``/app`` when ``frontend/dist-workspace``
  exists.

Safety: exposes only decision-support endpoints over synthetic patient data; it
is not a medical device and returns no medical advice.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from api.fhir import router as fhir_router
from api.middleware import RequestContextMiddleware
from api.routes import router
from api.v2 import MissingPersona
from api.v2 import router as v2_router
from src.agent.service import AgentBusy, RunConflict, SessionMismatch
from src.config import POSITIONING_STATEMENT, PROJECT_ROOT
from src.errors import NotFoundError
from src.governance.review import ReviewConflict, ReviewError
from src.persistence.db import utc_now
from src.retrieval.index import IsolationError
from src.runtime import close_runtime, get_runtime
from src.safety.policy import AuthorizationError
from src.tracing import configure_langsmith
from src import observability

WORKSPACE_DIST = PROJECT_ROOT / "frontend" / "dist-workspace"


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    configure_langsmith()
    observability.configure()
    try:
        yield
    finally:
        close_runtime()
        observability.shutdown()


def _log_request(request_id: str, method: str, route: str, status_code: int, latency_ms: float) -> None:
    get_runtime().db.execute(
        "INSERT INTO request_log(request_id, ts, method, route, status_code, latency_ms) VALUES (?,?,?,?,?,?)",
        (request_id, utc_now(), method, route, status_code, latency_ms),
    )


app = FastAPI(
    title="Clinical-Trace AI",
    description=POSITIONING_STATEMENT,
    version="2.0.0-dev",
    lifespan=lifespan,
)
app.add_middleware(RequestContextMiddleware, request_logger=_log_request)
app.include_router(router)
app.include_router(v2_router)
app.include_router(fhir_router)


def _error(request: Request, status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "message": message, "request_id": getattr(request.state, "request_id", None)}},
        status_code=status,
    )


@app.exception_handler(MissingPersona)
async def _missing_persona(request: Request, exc: MissingPersona) -> JSONResponse:
    return _error(request, 401, "persona_required", str(exc))


@app.exception_handler(RequestValidationError)
async def _invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
    if request.url.path.startswith(("/api/v2", "/fhir")):
        # Do not echo invalid source text or submitted identifiers into diagnostics.
        return _error(request, 422, "invalid_request", "request does not match the documented schema")
    return await request_validation_exception_handler(request, exc)


@app.exception_handler(AuthorizationError)
async def _forbidden(request: Request, exc: AuthorizationError) -> JSONResponse:
    return _error(request, 403, "forbidden", exc.decision.reason)


@app.exception_handler(SessionMismatch)
async def _session_mismatch(request: Request, exc: SessionMismatch) -> JSONResponse:
    return _error(request, 403, "session_mismatch", str(exc))


@app.exception_handler(RunConflict)
async def _run_conflict(request: Request, exc: RunConflict) -> JSONResponse:
    return _error(request, 409, "run_conflict", str(exc))


@app.exception_handler(AgentBusy)
async def _agent_busy(request: Request, exc: AgentBusy) -> JSONResponse:
    response = _error(request, 503, "agent_busy", str(exc))
    response.headers["Retry-After"] = "1"
    return response


@app.exception_handler(NotFoundError)
async def _not_found(request: Request, exc: NotFoundError) -> JSONResponse:
    return _error(request, 404, "not_found", str(exc))


@app.exception_handler(ReviewConflict)
async def _conflict(request: Request, exc: ReviewConflict) -> JSONResponse:
    return _error(request, 409, "conflict", str(exc))


@app.exception_handler(ReviewError)
async def _review_error(request: Request, exc: ReviewError) -> JSONResponse:
    return _error(request, 400, "invalid_review_action", str(exc))


@app.exception_handler(IsolationError)
async def _isolation(request: Request, _: IsolationError) -> JSONResponse:
    return _error(request, 500, "isolation_violation", "request failed closed: patient isolation check failed")


if WORKSPACE_DIST.exists():
    app.mount("/app", StaticFiles(directory=WORKSPACE_DIST, html=True), name="workspace")

    @app.get("/workspace", include_in_schema=False)
    def workspace_redirect() -> RedirectResponse:
        return RedirectResponse("/app/")
