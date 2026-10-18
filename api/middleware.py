"""Privacy-aware HTTP IDs, bounded request bodies, token buckets and diagnostics.

Only route templates, status and timing enter logs. Peer identity comes from
ASGI (configure trusted proxies in the server); arbitrary forwarded headers
cannot choose a bucket. Actual streamed bytes are limited before route parsing.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import threading
import time
import uuid
from typing import Any

from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src.observability import span

logger = logging.getLogger("clinical_trace.http")
MAX_BODY_BYTES = int(os.getenv("CLINICAL_TRACE_MAX_BODY_BYTES", str(32 * 1024)))
BODY_TIMEOUT_S = 10.0
PROTECTED_PREFIXES = ("/api/v2", "/fhir")
EDGE_CLIENT_PATTERN = re.compile(r"[0-9a-f]{8,64}")


class TokenBucketLimiter:
    """Per-peer buckets with idle eviction and a hard memory bound."""

    def __init__(self, per_minute: int, agent_per_minute: int, max_buckets: int = 20000) -> None:
        if min(per_minute, agent_per_minute, max_buckets) < 1:
            raise ValueError("rate limits and bucket capacity must be positive")
        self.limits = {"default": per_minute, "agent": agent_per_minute}
        self.max_buckets = max_buckets
        self._buckets: dict[tuple[str, str], tuple[float, float]] = {}
        self._lock = threading.Lock()
        self._last_cleanup = 0.0
        self.rejected = 0

    def allow(self, client: str, bucket: str) -> tuple[bool, float]:
        capacity = float(self.limits[bucket])
        rate = capacity / 60.0
        now = time.monotonic()
        key = (client, bucket)
        with self._lock:
            if now - self._last_cleanup >= 60:
                self._buckets = {k: v for k, v in self._buckets.items() if now - v[1] < 120}
                self._last_cleanup = now
            if key not in self._buckets and len(self._buckets) >= self.max_buckets:
                self.rejected += 1
                return False, 60.0
            tokens, last = self._buckets.get(key, (capacity, now))
            tokens = min(capacity, tokens + (now - last) * rate)
            if tokens < 1.0:
                self._buckets[key] = (tokens, now)
                self.rejected += 1
                return False, (1.0 - tokens) / rate
            self._buckets[key] = (tokens - 1.0, now)
            return True, 0.0

    def describe(self) -> dict[str, Any]:
        with self._lock:
            return {"requests_per_minute": self.limits["default"], "agent_runs_per_minute": self.limits["agent"],
                    "rejected_since_start": self.rejected, "tracked_clients": len({k[0] for k in self._buckets}),
                    "max_buckets": self.max_buckets}


RATE_LIMITER = TokenBucketLimiter(
    per_minute=int(os.getenv("CLINICAL_TRACE_RATE_LIMIT_PER_MIN", "120")),
    agent_per_minute=int(os.getenv("CLINICAL_TRACE_AGENT_RATE_LIMIT_PER_MIN", "20")),
)


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp, request_logger: Any = None) -> None:
        self.app = app
        self.request_logger = request_logger

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = str(uuid.uuid4())
        scope.setdefault("state", {})["request_id"] = request_id
        request = Request(scope)
        with span("clinical.http", {"request_id": request_id, "http.request.method": request.method},
                  request.headers.get("traceparent")) as current:
            if current is not None:
                scope["state"]["trace_id"] = format(current.get_span_context().trace_id, "032x")
            await self._http(scope, receive, send, current)

    async def _http(self, scope: Scope, receive: Receive, send: Send, current: Any) -> None:
        request_id = scope["state"]["request_id"]
        started = time.perf_counter()
        path = scope.get("path", "")
        protected = path.startswith(PROTECTED_PREFIXES)
        request = Request(scope)
        status_code = 500

        async def send_response(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = request_id
                if scope["state"].get("trace_id"):
                    headers["X-Trace-ID"] = scope["state"]["trace_id"]
                headers["X-Content-Type-Options"] = "nosniff"
                headers["Referrer-Policy"] = "no-referrer"
                if path.startswith("/app") and not path.startswith("/app/assets/"):
                    headers["Cache-Control"] = "no-cache"
            await send(message)

        async def reject(status: int, code: str, message: str, retry: float | None = None) -> None:
            headers = {"Retry-After": str(int(retry) + 1)} if retry is not None else None
            response = JSONResponse({"error": {"code": code, "message": message, "request_id": request_id}},
                                    status_code=status, headers=headers)
            await response(scope, receive, send_response)

        try:
            replay = receive
            if protected:
                length = request.headers.get("content-length")
                if length and (len(length) > 10 or not length.isdigit() or int(length) > MAX_BODY_BYTES):
                    await reject(413, "payload_too_large", f"request body exceeds {MAX_BODY_BYTES} bytes or has invalid length")
                    return
                execution = path in {"/api/v2/agent/runs", "/api/v2/agent/runs/stream"} or (
                    path.startswith("/api/v2/agent/runs/") and path.endswith("/resume"))
                bucket = "agent" if execution and request.method == "POST" else "default"
                # Uvicorn may set client from forwarded headers only for configured trusted proxies.
                peer = request.client.host if request.client else "unknown"
                edge_secret = os.getenv("CLINICAL_TRACE_EDGE_SECRET", "")
                if edge_secret and not (request.method == "GET" and path == "/api/v2/system/status"):
                    # Public deployments accept protected traffic only from their edge, which has already
                    # applied per-visitor limits; its salted client key then selects the bucket.
                    if not hmac.compare_digest(request.headers.get("x-edge-secret", ""), edge_secret):
                        await reject(403, "edge_required", "this deployment only accepts requests through its public edge")
                        return
                    edge_client = request.headers.get("x-edge-client", "")
                    if EDGE_CLIENT_PATTERN.fullmatch(edge_client):
                        peer = f"edge:{edge_client}"
                allowed, retry_after = RATE_LIMITER.allow(peer, bucket)
                if not allowed:
                    await reject(429, "rate_limited", "rate limit exceeded; retry shortly", retry_after)
                    return
                chunks, received = [], 0
                deadline = time.monotonic() + BODY_TIMEOUT_S
                while True:
                    try:
                        message = await asyncio.wait_for(receive(), timeout=max(0.001, deadline - time.monotonic()))
                    except asyncio.TimeoutError:
                        await reject(408, "request_timeout", "request body did not arrive within the time budget")
                        return
                    if message["type"] == "http.disconnect":
                        status_code = 499
                        return
                    chunk = message.get("body", b"")
                    received += len(chunk)
                    if received > MAX_BODY_BYTES:
                        await reject(413, "payload_too_large", f"request body exceeds {MAX_BODY_BYTES} bytes")
                        return
                    chunks.append(chunk)
                    if not message.get("more_body", False):
                        break
                buffered = b"".join(chunks)
                replayed = False

                async def replay() -> Message:
                    nonlocal replayed
                    if not replayed:
                        replayed = True
                        return {"type": "http.request", "body": buffered, "more_body": False}
                    return await receive()

            await self.app(scope, replay, send_response)
        finally:
            latency_ms = (time.perf_counter() - started) * 1000
            template = getattr(scope.get("route"), "path", None) or ("unmatched" if protected else "static")
            if current is not None:
                from opentelemetry.trace import StatusCode

                current.set_attribute("http.route", template)
                current.set_attribute("http.response.status_code", status_code)
                if status_code >= 500:
                    current.set_status(StatusCode.ERROR)
            if protected and self.request_logger is not None:
                try:
                    self.request_logger(request_id, request.method, template, status_code, latency_ms)
                except Exception:  # pragma: no cover - diagnostics never break a request
                    logger.error("request log write failed")
            logger.info(json.dumps({"event": "http_request", "request_id": request_id,
                                    "trace_id": scope["state"].get("trace_id"), "route": template,
                                    "status": status_code, "latency_ms": round(latency_ms, 2)}))
