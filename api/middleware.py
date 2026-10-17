"""HTTP middleware: request ids, privacy-aware request logging, rate limits, size limits.

Request logs store method, route *template* (never the concrete URL with ids
or query strings), status, and latency. No bodies or headers are logged.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logger = logging.getLogger("clinical_trace.http")
MAX_BODY_BYTES = int(os.getenv("CLINICAL_TRACE_MAX_BODY_BYTES", str(32 * 1024)))
PROTECTED_PREFIXES = ("/api/v2", "/fhir")


class TokenBucketLimiter:
    """Per-client token buckets; ``agent`` runs have a stricter bucket."""

    def __init__(self, per_minute: int, agent_per_minute: int) -> None:
        self.limits = {"default": per_minute, "agent": agent_per_minute}
        self._buckets: dict[tuple[str, str], tuple[float, float]] = {}
        self._lock = threading.Lock()
        self.rejected = 0

    def allow(self, client: str, bucket: str) -> tuple[bool, float]:
        capacity = float(self.limits[bucket])
        rate = capacity / 60.0
        now = time.monotonic()
        with self._lock:
            tokens, last = self._buckets.get((client, bucket), (capacity, now))
            tokens = min(capacity, tokens + (now - last) * rate)
            if tokens < 1.0:
                self._buckets[(client, bucket)] = (tokens, now)
                self.rejected += 1
                return False, (1.0 - tokens) / rate
            self._buckets[(client, bucket)] = (tokens - 1.0, now)
            return True, 0.0

    def describe(self) -> dict[str, Any]:
        return {"requests_per_minute": self.limits["default"], "agent_runs_per_minute": self.limits["agent"],
                "rejected_since_start": self.rejected, "tracked_clients": len({k[0] for k in self._buckets})}


RATE_LIMITER = TokenBucketLimiter(
    per_minute=int(os.getenv("CLINICAL_TRACE_RATE_LIMIT_PER_MIN", "120")),
    agent_per_minute=int(os.getenv("CLINICAL_TRACE_AGENT_RATE_LIMIT_PER_MIN", "20")),
)


def _client_key(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return request.client.host if request.client else "unknown"


def _error(status: int, code: str, message: str, request_id: str, headers: dict[str, str] | None = None) -> Response:
    return JSONResponse({"error": {"code": code, "message": message, "request_id": request_id}},
                        status_code=status, headers=headers)


class RequestContextMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: Any, request_logger: Any = None) -> None:
        super().__init__(app)
        self.request_logger = request_logger

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = str(uuid.uuid4())
        request.state.request_id = request_id
        started = time.perf_counter()
        path = request.url.path
        protected = path.startswith(PROTECTED_PREFIXES)
        if protected:
            length = request.headers.get("content-length")
            if length and length.isdigit() and int(length) > MAX_BODY_BYTES:
                return _error(413, "payload_too_large", f"request body exceeds {MAX_BODY_BYTES} bytes", request_id)
            bucket = "agent" if path == "/api/v2/agent/runs" and request.method == "POST" else "default"
            allowed, retry_after = RATE_LIMITER.allow(_client_key(request), bucket)
            if not allowed:
                return _error(429, "rate_limited", "rate limit exceeded; retry shortly", request_id,
                              {"Retry-After": str(int(retry_after) + 1)})
        response = await call_next(request)
        latency_ms = (time.perf_counter() - started) * 1000
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        route = request.scope.get("route")
        template = getattr(route, "path", None) or ("static" if not protected else "unmatched")
        if protected and self.request_logger is not None:
            try:
                self.request_logger(request_id, request.method, template, response.status_code, latency_ms)
            except Exception:  # pragma: no cover - logging must never break a request
                logger.exception("request log write failed")
        logger.info("request", extra={"request_id": request_id, "route": template, "status": response.status_code,
                                      "latency_ms": round(latency_ms, 2)})
        return response
