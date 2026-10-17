"""SSE progress followed by one verified result; unverified text is never streamed."""

from __future__ import annotations

import asyncio
import json
import queue
import threading
from contextvars import copy_context
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

from api.v2_schemas import AgentRunRequest, AgentRunResponse
from src.agent.graph import CancelledRun
from src.agent.service import AgentBusy, RunConflict
from src.errors import NotFoundError
from src.runtime import Runtime
from src.safety.policy import AccessContext, Action


async def stream_run(
    request: Request, body: AgentRunRequest, ctx: AccessContext, rt: Runtime, idempotency_key: str | None
) -> Any:
    rt.policy.require(ctx, Action.AGENT_RUN, body.patient_id)
    if rt.records.profile(body.patient_id) is None:
        raise NotFoundError("patient not found")
    # The graph has at most 40 steps. A bounded queue prevents slow clients accumulating state.
    events: queue.Queue = queue.Queue(maxsize=64)
    stopped = threading.Event()
    run_ids: list[str] = []

    def progress(event: dict[str, Any]) -> None:
        if stopped.is_set():
            raise CancelledRun(event["run_id"])
        if not run_ids:
            run_ids.append(event["run_id"])
        events.put_nowait(("progress", event))

    def worker() -> None:
        try:
            result = rt.agent.run(
                ctx, body.patient_id, body.question, body.session_id, idempotency_key=idempotency_key, observer=progress
            )
            validated = AgentRunResponse.model_validate(result).model_dump()
            events.put_nowait(("result", validated))
        except Exception as exc:
            code, status = (
                ("agent_busy", 503)
                if isinstance(exc, AgentBusy)
                else ("run_conflict", 409)
                if isinstance(exc, RunConflict)
                else ("stream_error", 500)
            )
            events.put_nowait(
                (
                    "error",
                    {
                        "code": code,
                        "status": status,
                        "message": "execution unavailable; inspect status or start a new request",
                        "request_id": ctx.request_id,
                    },
                )
            )
        finally:
            events.put_nowait(("done", None))

    def cancel() -> None:
        stopped.set()
        if run_ids:
            try:
                rt.agent.cancel(ctx, run_ids[0])
            except (PermissionError, NotFoundError):
                pass

    def poll() -> Any:
        try:
            return events.get(timeout=0.25)
        except queue.Empty:
            return None

    context = copy_context()
    threading.Thread(target=context.run, args=(worker,), daemon=True, name="agent-stream").start()
    # Wait for a real first event before sending headers so capacity/conflict errors retain HTTP semantics.
    first = None
    while first is None:
        if await request.is_disconnected():
            cancel()
            return JSONResponse(
                {"error": {"code": "client_closed", "message": "request disconnected"}}, status_code=499
            )
        first = await asyncio.to_thread(poll)
    if first[0] == "error":
        cancel()
        return JSONResponse(
            {"error": first[1]},
            status_code=first[1]["status"],
            headers={"Retry-After": "1"} if first[1]["status"] == 503 else None,
        )

    async def generate():
        finished = False
        event = first
        try:
            while True:
                if await request.is_disconnected():
                    return
                if event is None:
                    event = await asyncio.to_thread(poll)
                    if event is None:
                        yield ": waiting\n\n"
                        continue
                kind, payload = event
                if kind == "done":
                    return
                yield f"event: {kind}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"
                if kind in {"result", "error"}:
                    finished = True
                    return
                event = None
        finally:
            if not finished:
                cancel()

    return StreamingResponse(
        generate(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"}
    )
