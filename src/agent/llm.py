"""Language-model provider abstraction for the agent.

``OpenAICompatibleProvider`` speaks the ``/v1/chat/completions`` protocol, so
the same code drives a local Ollama server, a self-hosted llama.cpp/vLLM
server, or a hosted OpenAI-compatible endpoint. No provider is enabled unless
configured; without one the agent runs in *deterministic* mode and says so.

Configuration (environment):

* ``CLINICAL_TRACE_LLM_PROVIDER`` - ``none`` (default), ``ollama``, ``openai-compatible``, or
  ``cloudflare-workers-ai`` (the public edge's OpenAI-compatible Workers AI proxy)
* ``CLINICAL_TRACE_LLM_BASE_URL`` - e.g. ``http://127.0.0.1:11434/v1`` for Ollama
* ``CLINICAL_TRACE_LLM_MODEL`` - e.g. ``qwen2.5:1.5b``
* ``CLINICAL_TRACE_LLM_API_KEY`` - optional bearer token (never logged)
* ``CLINICAL_TRACE_LLM_TIMEOUT_S`` - request timeout (default 60)
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import httpx


class ProviderUnavailable(RuntimeError):
    pass


class ProviderQuotaExhausted(ProviderUnavailable):
    """The provider's free allocation is used up; calls fail fast until ``retry_after_s`` elapses."""

    def __init__(self, message: str, retry_after_s: float) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


@dataclass
class CircuitBreaker:
    """Opens after ``threshold`` consecutive failures; half-opens after ``cooldown_s``."""

    threshold: int = 3
    cooldown_s: float = 60.0
    failures: int = 0
    opened_at: float | None = None
    _probe_in_flight: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def allow(self) -> bool:
        with self._lock:
            if self.opened_at is None:
                return True
            if time.monotonic() - self.opened_at >= self.cooldown_s:
                if not self._probe_in_flight:
                    self._probe_in_flight = True
                    return True
            return False

    def record(self, success: bool) -> None:
        with self._lock:
            self._probe_in_flight = False
            if success:
                self.failures, self.opened_at = 0, None
            else:
                self.failures += 1
                if self.failures >= self.threshold:
                    self.opened_at = time.monotonic()

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        return "half-open" if time.monotonic() - self.opened_at >= self.cooldown_s else "open"


@dataclass
class Completion:
    text: str
    latency_ms: float
    prompt_tokens: int | None
    completion_tokens: int | None


class OpenAICompatibleProvider:
    def __init__(
        self,
        base_url: str,
        model: str,
        provider_name: str = "openai-compatible",
        api_key: str | None = None,
        timeout_s: float = 60.0,
        max_retries: int = 1,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.name = provider_name
        self.max_retries = max_retries
        self.breaker = CircuitBreaker()
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.Client(timeout=timeout_s, headers=headers, transport=transport)
        self.calls = 0
        self.failures = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.retries = 0
        self.neurons = 0.0
        self.quota_rejections = 0
        self._quota_until: float | None = None

    @property
    def quota_state(self) -> str:
        if self._quota_until is not None and time.monotonic() < self._quota_until:
            return "exhausted"
        return "available"

    def describe(self) -> dict[str, Any]:
        free_allocation = self.name == "cloudflare-workers-ai"
        return {"provider": self.name, "model": self.model, "circuit": self.breaker.state,
                "calls": self.calls, "failures": self.failures,
                "prompt_tokens": self.prompt_tokens, "completion_tokens": self.completion_tokens,
                "retries": self.retries, "quota_state": self.quota_state, "quota_rejections": self.quota_rejections,
                "neurons": round(self.neurons, 2) if free_allocation else None,
                "estimated_cost_usd": 0.0 if self.name in {"ollama", "mock"} or free_allocation else None,
                "cost_note": ("Workers AI calls are capped below the 10,000 neuron/day free allocation by the edge; "
                              "over-budget calls are refused, never billed" if free_allocation else
                              "local calls have no API charge; hosted pricing is unknown unless configured")}

    def _quota_rejection(self, response: httpx.Response) -> ProviderQuotaExhausted:
        try:
            retry_after = float(response.headers.get("retry-after") or 3600)
        except ValueError:
            retry_after = 3600.0
        retry_after = min(max(retry_after, 1.0), 86400.0)
        self._quota_until = time.monotonic() + retry_after
        self.quota_rejections += 1
        return ProviderQuotaExhausted("provider free quota exhausted", retry_after)

    def close(self) -> None:
        self._client.close()

    def complete(self, system: str, user: str, max_tokens: int = 700, json_mode: bool = True) -> Completion:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0,
            "max_tokens": max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        last: Exception | None = None
        if self.quota_state == "exhausted":
            remaining = (self._quota_until or 0) - time.monotonic()
            self.quota_rejections += 1
            raise ProviderQuotaExhausted("provider free quota exhausted", max(remaining, 1.0))
        for attempt in range(self.max_retries + 1):
            if not self.breaker.allow():
                raise ProviderUnavailable("circuit open or recovery probe already in flight")
            if attempt:
                self.retries += 1
            started = time.perf_counter()
            self.calls += 1
            try:
                response = self._client.post(f"{self.base_url}/chat/completions", json=payload)
                if response.status_code == 429 and _error_code(response) == "quota_exhausted":
                    # Quota exhaustion is a healthy provider saying "not today": no retry, no breaker trip.
                    self.breaker.record(True)
                    raise self._quota_rejection(response)
                if response.status_code in (400, 401, 403, 404, 413, 422):
                    self.failures += 1
                    self.breaker.record(False)
                    raise ProviderUnavailable(f"provider rejected the request (HTTP {response.status_code})")
                response.raise_for_status()
                body = response.json()
                text = body["choices"][0]["message"]["content"] or ""
                usage = body.get("usage") or {}
                self.prompt_tokens += int(usage.get("prompt_tokens") or 0)
                self.completion_tokens += int(usage.get("completion_tokens") or 0)
                neurons = usage.get("neurons", (body.get("edge") or {}).get("estimated_neurons"))
                if isinstance(neurons, (int, float)):
                    self.neurons += float(neurons)
                self.breaker.record(True)
                return Completion(text, (time.perf_counter() - started) * 1000,
                                  usage.get("prompt_tokens"), usage.get("completion_tokens"))
            except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
                last = exc
                self.failures += 1
                self.breaker.record(False)
                if attempt < self.max_retries:
                    time.sleep(0.5 * (2 ** attempt))
        raise ProviderUnavailable(f"provider request failed: {type(last).__name__}")


def _error_code(response: httpx.Response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    error = body.get("error") if isinstance(body, dict) else None
    return error.get("code") if isinstance(error, dict) else None


def extract_json(text: str) -> Any:
    """Parse a JSON object from model output, tolerating code fences and prose."""
    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.S)
    if fence:
        cleaned = fence.group(1).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start >= 0 and end > start:
            return json.loads(cleaned[start:end + 1])
        raise


def provider_from_env() -> OpenAICompatibleProvider | None:
    kind = os.getenv("CLINICAL_TRACE_LLM_PROVIDER", "none").strip().lower()
    if kind in ("", "none", "off"):
        return None
    model = os.getenv("CLINICAL_TRACE_LLM_MODEL", "qwen2.5:1.5b")
    default_url = "http://127.0.0.1:11434/v1" if kind == "ollama" else ""
    base_url = os.getenv("CLINICAL_TRACE_LLM_BASE_URL", default_url)
    if not base_url:
        return None
    return OpenAICompatibleProvider(
        base_url=base_url,
        model=model,
        provider_name=kind,
        api_key=os.getenv("CLINICAL_TRACE_LLM_API_KEY") or None,
        timeout_s=float(os.getenv("CLINICAL_TRACE_LLM_TIMEOUT_S", "60")),
    )
