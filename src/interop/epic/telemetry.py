"""Content-free diagnostics for the Epic sandbox adapter.

Counters and latencies are keyed only by operation, FHIR resource type and
categorical outcome codes. They never contain tokens, keys, patient identifiers,
names or clinical text. Spans reuse the application's opt-in local OpenTelemetry
helper and its attribute allow-list.
"""

from __future__ import annotations

import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

from src import observability

_SAFE_CODE = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
_MAX_SAMPLES = 512


def safe_code(value: object) -> str:
    """Reduce an external code to a short token that is safe to use as a counter label."""
    text = str(value) if value is not None else ""
    return text if _SAFE_CODE.fullmatch(text) else "other"


@dataclass
class OperationResult:
    status_code: int | None = None
    ok: bool = False
    error_type: str | None = None


class EpicTelemetry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, int] = {}
        self._latency: dict[str, list[float]] = {}

    def count(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + amount

    def _observe(self, operation: str, elapsed_ms: float) -> None:
        with self._lock:
            samples = self._latency.setdefault(operation, [])
            if len(samples) < _MAX_SAMPLES:
                samples.append(elapsed_ms)

    @contextmanager
    def operation(self, operation: str, resource_type: str | None = None) -> Iterator[OperationResult]:
        """Time one external call and record its categorical outcome."""
        label = f"{operation}.{resource_type}" if resource_type else operation
        result = OperationResult()
        started = time.perf_counter()
        with observability.span(f"clinical.epic.{label}", {"tool": "epic-sandbox"}) as current:
            try:
                yield result
            finally:
                elapsed = (time.perf_counter() - started) * 1000
                self._observe(label, elapsed)
                outcome = "ok" if result.ok else f"error.{safe_code(result.error_type or 'unknown')}"
                self.count(f"{label}.{outcome}")
                if current is not None:
                    current.set_attribute("ok", result.ok)
                    if result.status_code is not None:
                        current.set_attribute("http.response.status_code", result.status_code)
                    if result.error_type:
                        current.set_attribute("error_type", safe_code(result.error_type))

    def describe(self) -> dict[str, Any]:
        with self._lock:
            latency = {}
            for label, samples in self._latency.items():
                ordered = sorted(samples)
                latency[label] = {
                    "count": len(ordered),
                    "p50_ms": round(ordered[len(ordered) // 2], 2),
                    "p95_ms": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 2),
                }
            return {"counters": dict(sorted(self._counters.items())), "latency": latency}
