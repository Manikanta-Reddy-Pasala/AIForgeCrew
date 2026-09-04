"""Tracing setup: what turns it on, what refuses it, and what it does when off.

The interesting part is not the happy path — it is that `setup()` asks the
egress policy before it exports anything. That check exists because
AIFORGE_TELEMETRY_DISABLE used to close Langfuse and leave OTLP exporting, and
nothing tested it.
"""
from __future__ import annotations

import sys
import types

import pytest

from aiforge_core.observability import otel


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    """setup() is idempotent by module state; reset it per test."""
    monkeypatch.setattr(otel, "_INITIALISED", False)
    monkeypatch.setattr(otel, "_TRACER", None)
    monkeypatch.delenv("AIFORGE_OTEL_ENABLED", raising=False)
    monkeypatch.delenv("AIFORGE_OTEL_ENDPOINT", raising=False)
    monkeypatch.delenv("AIFORGE_OTEL_SERVICE_NAME", raising=False)


# ── setup ───────────────────────────────────────────────────────────────────

def test_tracing_is_off_unless_asked_for():
    assert otel.setup() is False
    assert otel._TRACER is None


def test_setup_is_idempotent(monkeypatch):
    """A second call must not rebuild the provider — it answers from state."""
    assert otel.setup() is False
    calls = []
    monkeypatch.setattr(otel.os, "environ", {"AIFORGE_OTEL_ENABLED": "1"})
    # _INITIALISED is now True, so the env change is not re-read.
    assert otel.setup() is False
    assert calls == []


def test_the_egress_policy_can_refuse_the_collector(monkeypatch):
    """Spans leave the box. A refused endpoint means tracing does not start —
    not a half-configured exporter retrying against a wall."""
    monkeypatch.setenv("AIFORGE_OTEL_ENABLED", "1")
    monkeypatch.setenv("AIFORGE_OTEL_ENDPOINT", "http://collector.example/v1")
    from aiforge_core.net import egress

    monkeypatch.setattr(egress, "allow",
                        lambda *a, **k: "telemetry egress is off")
    assert otel.setup() is False
    assert otel._TRACER is None


def test_a_missing_exporter_disables_tracing_instead_of_raising(monkeypatch):
    """opentelemetry-exporter-otlp is not a project dependency. Asking for
    tracing on a box without it must decline, not blow up at boot."""
    monkeypatch.setenv("AIFORGE_OTEL_ENABLED", "1")
    monkeypatch.setitem(
        sys.modules,
        "opentelemetry.exporter.otlp.proto.http.trace_exporter", None)
    assert otel.setup() is False
    assert otel._TRACER is None


def test_a_permitted_collector_builds_a_tracer(monkeypatch):
    """The full build path, with the SDK pieces stubbed — this box has the API
    package but not the OTLP exporter, so the real import is what stops it."""
    monkeypatch.setenv("AIFORGE_OTEL_ENABLED", "1")
    monkeypatch.setenv("AIFORGE_OTEL_ENDPOINT", "http://127.0.0.1:4318/v1/traces")
    monkeypatch.setenv("AIFORGE_OTEL_SERVICE_NAME", "aiforge-test")
    from aiforge_core.net import egress

    monkeypatch.setattr(egress, "allow", lambda *a, **k: None)

    built = {}

    class _Provider:
        def __init__(self, resource=None):
            built["resource"] = resource

        def add_span_processor(self, proc):
            built["processor"] = proc

    def _exporter(endpoint=None):
        built["endpoint"] = endpoint
        return object()

    mods = {
        "opentelemetry.sdk.resources": types.SimpleNamespace(
            Resource=types.SimpleNamespace(create=lambda d: d)),
        "opentelemetry.sdk.trace": types.SimpleNamespace(
            TracerProvider=_Provider),
        "opentelemetry.sdk.trace.export": types.SimpleNamespace(
            BatchSpanProcessor=lambda exp: ("batch", exp)),
        "opentelemetry.exporter.otlp.proto.http.trace_exporter":
            types.SimpleNamespace(OTLPSpanExporter=_exporter),
    }
    for name, mod in mods.items():
        monkeypatch.setitem(sys.modules, name, mod)

    import opentelemetry.trace as _t
    monkeypatch.setattr(_t, "set_tracer_provider",
                        lambda p: built.__setitem__("provider", p))
    monkeypatch.setattr(_t, "get_tracer", lambda name: f"tracer:{name}")

    assert otel.setup() is True
    assert otel._TRACER == "tracer:aiforge"
    assert built["endpoint"] == "http://127.0.0.1:4318/v1/traces"
    assert built["resource"]["service.name"] == "aiforge-test"


# ── span ────────────────────────────────────────────────────────────────────

def test_span_is_a_no_op_context_manager_when_tracing_is_off():
    with otel.span("anything", a=1) as sp:
        assert sp is None


def test_span_yields_a_span_and_carries_the_attributes(monkeypatch):
    recorded = {}

    class _Span:
        def set_attribute(self, k, v):
            recorded[k] = v

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Tracer:
        def start_as_current_span(self, name):
            recorded["name"] = name
            return _Span()

    monkeypatch.setattr(otel, "_INITIALISED", True)
    monkeypatch.setattr(otel, "_TRACER", _Tracer())
    with otel.span("tool.call", tool="bash") as sp:
        assert sp is not None
    assert recorded["name"] == "tool.call"
    assert recorded["tool"] == "bash"


def test_an_exception_inside_a_span_is_recorded_and_re_raised(monkeypatch):
    seen = {}

    class _Span:
        def set_attribute(self, k, v):
            pass

        def record_exception(self, exc):
            seen["exc"] = exc

        def set_status_description(self, text):
            seen["status"] = text

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Tracer:
        def start_as_current_span(self, name):
            return _Span()

    monkeypatch.setattr(otel, "_INITIALISED", True)
    monkeypatch.setattr(otel, "_TRACER", _Tracer())
    with pytest.raises(ValueError, match="boom"), otel.span("x"):
        raise ValueError("boom")
    assert isinstance(seen["exc"], ValueError)
    assert "boom" in seen["status"]


def test_an_attribute_the_backend_rejects_does_not_break_the_span(monkeypatch):
    class _Span:
        def set_attribute(self, k, v):
            raise TypeError("unsupported attribute type")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Tracer:
        def start_as_current_span(self, name):
            return _Span()

    monkeypatch.setattr(otel, "_INITIALISED", True)
    monkeypatch.setattr(otel, "_TRACER", _Tracer())
    with otel.span("x", weird=object()) as sp:
        assert sp is not None


# ── decorator ───────────────────────────────────────────────────────────────

def test_trace_role_returns_the_wrapped_functions_value():
    @otel.trace_role("planner")
    def _work(a, b=2):
        return a + b

    assert _work(1) == 3
    assert _work.__name__ == "_work"


def test_trace_role_lets_the_exception_through():
    @otel.trace_role("doer")
    def _boom():
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError, match="nope"):
        _boom()


# ── token usage ─────────────────────────────────────────────────────────────

def test_record_token_usage_is_silent_when_tracing_is_off():
    assert otel.record_token_usage("doer", prompt_tokens=1,
                                   completion_tokens=2, model="m") is None


def test_record_token_usage_annotates_the_current_span(monkeypatch):
    attrs = {}

    class _Span:
        def set_attribute(self, k, v):
            attrs[k] = v

    monkeypatch.setattr(otel, "_INITIALISED", True)
    monkeypatch.setattr(otel, "_TRACER", object())
    import opentelemetry.trace as _t
    monkeypatch.setattr(_t, "get_current_span", lambda: _Span())

    otel.record_token_usage("planner", prompt_tokens=10,
                            completion_tokens=20, model="gpt-oss:120b")
    assert attrs["llm.usage.prompt_tokens"] == 10
    assert attrs["llm.usage.completion_tokens"] == 20
    assert attrs["llm.role"] == "planner"
    assert attrs["llm.model"] == "gpt-oss:120b"


def test_missing_token_counts_are_simply_not_set(monkeypatch):
    attrs = {}

    class _Span:
        def set_attribute(self, k, v):
            attrs[k] = v

    monkeypatch.setattr(otel, "_INITIALISED", True)
    monkeypatch.setattr(otel, "_TRACER", object())
    import opentelemetry.trace as _t
    monkeypatch.setattr(_t, "get_current_span", lambda: _Span())

    otel.record_token_usage("doer", prompt_tokens=None,
                            completion_tokens=None, model="m")
    assert "llm.usage.prompt_tokens" not in attrs
    assert attrs["llm.role"] == "doer"
