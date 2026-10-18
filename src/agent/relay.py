"""Inference relay through the public edge.

Some hosts (notably Hugging Face Spaces) cannot open outbound connections to Cloudflare, while
the Cloudflare edge can always reach the backend. In relay mode the backend never calls the
model itself: during a streamed agent run, each chat-completion request is emitted on the run's
SSE stream as an ``event: relay`` frame. The edge intercepts that frame (it is never shown to the
browser), runs the completion on Workers AI under its budget, and posts the OpenAI-shaped result
to ``POST /api/v2/agent/relay/{relay_id}`` with the edge secret. The waiting provider call then
continues exactly as if it had made the HTTP request, so retries, quota handling, verification and
labelling are unchanged.

Requests that are not streamed through the edge have no relay channel; they fail fast with
``RelayChannelUnavailable`` and the agent answers in its labelled deterministic mode.
"""

from __future__ import annotations

import json
import secrets
import threading
from contextvars import ContextVar
from typing import Any, Callable

import httpx

from src.agent.llm import OpenAICompatibleProvider, ProviderUnavailable

RelayChannel = Callable[[dict[str, Any]], None]
RELAY_CHANNEL: ContextVar[RelayChannel | None] = ContextVar("clinical_trace_relay_channel", default=None)


class RelayChannelUnavailable(ProviderUnavailable):
    """No edge stream is attached to this request, so live inference cannot be relayed."""


class RelayHub:
    """Pending relayed completions, keyed by unguessable ids; bounded to avoid unbounded waits."""

    def __init__(self, max_pending: int = 64) -> None:
        self.max_pending = max_pending
        self._pending: dict[str, tuple[threading.Event, list[tuple[int, Any]]]] = {}
        self._lock = threading.Lock()

    def call(self, channel: RelayChannel, payload: dict[str, Any], timeout_s: float) -> tuple[int, Any]:
        relay_id = secrets.token_urlsafe(18)
        done = threading.Event()
        slot: list[tuple[int, Any]] = []
        with self._lock:
            if len(self._pending) >= self.max_pending:
                raise httpx.ConnectError("relay capacity exhausted")
            self._pending[relay_id] = (done, slot)
        try:
            channel({"relay_id": relay_id, "request": payload})
            if not done.wait(timeout_s):
                raise httpx.ReadTimeout("edge did not return a relayed completion in time")
            return slot[0]
        finally:
            with self._lock:
                self._pending.pop(relay_id, None)

    def fulfil(self, relay_id: str, status: int, body: Any) -> bool:
        with self._lock:
            entry = self._pending.get(relay_id)
            if entry is None or entry[0].is_set():
                return False
            entry[1].append((status, body))
            entry[0].set()
            return True

    def pending(self) -> int:
        with self._lock:
            return len(self._pending)


HUB = RelayHub()


class RelayTransport(httpx.BaseTransport):
    """httpx transport that sends chat-completion requests over the current run's edge stream."""

    def __init__(self, hub: RelayHub = HUB, timeout_s: float = 60.0) -> None:
        self.hub = hub
        self.timeout_s = timeout_s

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        channel = RELAY_CHANNEL.get()
        if channel is None:
            raise httpx.ConnectError("no edge relay channel", request=request)
        status, body = self.hub.call(channel, json.loads(request.content), self.timeout_s)
        return httpx.Response(int(status), json=body, request=request)


class EdgeRelayProvider(OpenAICompatibleProvider):
    """Workers AI through the edge relay; refuses immediately when no relay channel exists."""

    def complete(self, system: str, user: str, max_tokens: int = 700, json_mode: bool = True):  # type: ignore[override]
        if RELAY_CHANNEL.get() is None:
            # Not a provider failure: do not count it against the circuit breaker.
            raise RelayChannelUnavailable("live inference is relayed only on streamed requests through the public edge")
        return super().complete(system, user, max_tokens=max_tokens, json_mode=json_mode)


def relay_provider(model: str, timeout_s: float = 60.0, hub: RelayHub = HUB) -> EdgeRelayProvider:
    return EdgeRelayProvider(
        "http://edge-relay/v1",
        model,
        provider_name="cloudflare-workers-ai",
        transport=RelayTransport(hub, timeout_s),
        timeout_s=timeout_s,
        max_retries=1,
    )
