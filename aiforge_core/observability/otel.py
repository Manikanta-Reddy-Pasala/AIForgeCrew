"""OpenTelemetry tracing — single entry point for AIForge spans.

Goals (KISS):
- One ``setup()`` call at process boot. No-op when otel deps aren't
  installed so dev boxes skip cleanly.
- ``@trace_role(role)`` decorator wraps long-running role entry
  points (chat_ask, doer.run_doer_via_ga, planner.run_planner).
- ``span(name, **attrs)`` context-manager for finer-grained spans
  (single tool call, single LLM round-trip).

Configuration via env:
- ``AIFORGE_OTEL_ENABLED=1`` — turn on (default off)
- ``AIFORGE_OTEL_ENDPOINT`` — OTLP HTTP collector URL
  (default http://127.0.0.1:4318/v1/traces)
- ``AIFORGE_OTEL_SERVICE_NAME`` — service.name attribute
  (default aiforge)

Public surface:
- ``setup() -> None``
- ``span(name, **attrs)`` context-manager
- ``trace_role(role)`` decorator
- ``record_token_usage(role, prompt, completion, model)`` shorthand
"""
from __future__ import annotations

import contextlib
import functools
import os
from typing import Iterator


_INITIALISED = False
_TRACER = None
_NOOP_SPAN_CONTEXT = None


def setup() -> bool:
    """Initialise the global tracer. Idempotent. Returns True on
    success, False if otel isn't installed or disabled."""
    global _INITIALISED, _TRACER
    if _INITIALISED:
        return _TRACER is not None
    _INITIALISED = True
    if os.environ.get("AIFORGE_OTEL_ENABLED", "0") != "1":
        return False
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
    except ImportError:
        return False

    endpoint = os.environ.get(
        "AIFORGE_OTEL_ENDPOINT", "http://127.0.0.1:4318/v1/traces",
    )
    service_name = os.environ.get("AIFORGE_OTEL_SERVICE_NAME", "aiforge")
    provider = TracerProvider(resource=Resource.create({
        "service.name": service_name,
        "service.namespace": "aiforge",
    }))
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)),
    )
    trace.set_tracer_provider(provider)
    _TRACER = trace.get_tracer("aiforge")
    return True


@contextlib.contextmanager
def span(name: str, **attrs) -> Iterator[object]:
    """Create one span. No-op when otel disabled / not installed."""
    setup()
    if _TRACER is None:
        yield None
        return
    with _TRACER.start_as_current_span(name) as sp:
        for k, v in attrs.items():
            try:
                sp.set_attribute(k, v)
            except Exception:
                pass
        try:
            yield sp
        except Exception as exc:
            try:
                sp.record_exception(exc)
                sp.set_status_description(str(exc)[:200])
            except Exception:
                pass
            raise


def trace_role(role: str):
    """Decorator: wrap a role entry-point in a single span."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            with span(f"role.{role}", role=role):
                return fn(*args, **kwargs)
        return wrapper
    return deco


def record_token_usage(
    role: str, *,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    model: str,
) -> None:
    """Annotate the current span with token-usage attributes."""
    setup()
    if _TRACER is None:
        return
    try:
        from opentelemetry import trace as _t
        sp = _t.get_current_span()
        if prompt_tokens is not None:
            sp.set_attribute("llm.usage.prompt_tokens", int(prompt_tokens))
        if completion_tokens is not None:
            sp.set_attribute(
                "llm.usage.completion_tokens", int(completion_tokens),
            )
        sp.set_attribute("llm.role", role)
        sp.set_attribute("llm.model", model)
    except Exception:
        pass
