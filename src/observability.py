"""Opt-in local OpenTelemetry spans with an explicit content-free attribute set.

No network exporter or paid service is activated. Trace files are private
runtime artifacts. No exception messages, prompts, bodies or source text.
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor
from opentelemetry.trace import StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

_provider: TracerProvider | None = None
_output: Any = None
_lock = threading.Lock()
ALLOWED_ATTRIBUTES = frozenset(
    {
        "request_id",
        "run_id",
        "node",
        "tool",
        "ok",
        "error_type",
        "http.request.method",
        "http.route",
        "http.response.status_code",
    }
)


def configure(path: Path | None = None) -> None:
    global _provider, _output
    configured = str(path) if path is not None else os.getenv("CLINICAL_TRACE_OTEL_FILE", "")
    if not configured:
        return
    with _lock:
        if _provider is not None:
            return
        destination = Path(configured)
        destination.parent.mkdir(parents=True, exist_ok=True)
        _output = destination.open("a", encoding="utf-8")
        _provider = TracerProvider(resource=Resource({"service.name": "clinical-trace-ai"}))
        exporter = ConsoleSpanExporter(out=_output, formatter=lambda span: span.to_json(indent=None) + "\n")
        _provider.add_span_processor(SimpleSpanProcessor(exporter))


def shutdown() -> None:
    global _provider, _output
    with _lock:
        if _provider is not None:
            _provider.shutdown()
            _provider = None
        if _output is not None:
            _output.close()
            _output = None


def describe() -> dict[str, Any]:
    return {
        "enabled": _provider is not None,
        "exporter": "local OpenTelemetry JSONL" if _provider else "none",
        "network_export": False,
        "source_text_recorded": False,
    }


@contextmanager
def span(name: str, attributes: dict[str, Any], traceparent: str | None = None) -> Iterator[Any]:
    if _provider is None:
        yield None
        return
    safe = {k: v for k, v in attributes.items() if k in ALLOWED_ATTRIBUTES and v is not None}
    context = TraceContextTextMapPropagator().extract({"traceparent": traceparent}) if traceparent else None
    with _provider.get_tracer("clinical_trace").start_as_current_span(
        name,
        context=context,
        attributes=safe,
        record_exception=False,
        set_status_on_exception=False,
    ) as current:
        try:
            yield current
        except BaseException:
            current.set_status(StatusCode.ERROR)
            raise
