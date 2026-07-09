"""Langfuse mirror — env-gated, soft-fail, never touches the call result."""
from __future__ import annotations

from aiforge_core.integrations import langfuse_adapter as lf


def test_disabled_without_keys(monkeypatch):
    for k in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert lf.enabled() is False


def test_kill_switch_wins(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    monkeypatch.setenv("AIFORGE_LANGFUSE_DISABLE", "1")
    assert lf.enabled() is False


def test_complete_unaffected_when_tracing_off(monkeypatch):
    """client.complete returns the model output verbatim with tracing unset —
    and a tracing crash can never propagate."""
    for k in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        monkeypatch.delenv(k, raising=False)
    from aiforge_core.llm import client
    monkeypatch.setattr(client, "_complete_impl",
                        lambda role, messages, **k2: "the answer")
    assert client.complete("chat", [{"role": "user", "content": "q"}]) \
        == "the answer"


def test_trace_crash_never_breaks_turn(monkeypatch):
    from aiforge_core.llm import client

    def boom(**k):
        raise RuntimeError("langfuse down")

    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    monkeypatch.delenv("AIFORGE_LANGFUSE_DISABLE", raising=False)
    monkeypatch.setattr(lf, "available", lambda: True)
    monkeypatch.setattr(lf, "record_generation", boom)
    monkeypatch.setattr(client, "_complete_impl",
                        lambda role, messages, **k2: "still fine")
    assert client.complete("chat", [{"role": "user", "content": "q"}]) \
        == "still fine"


def test_record_payload_shape(monkeypatch):
    """Adapter builds the v2 generation payload from our shapes."""
    calls = {}

    class _FakeLF:
        def generation(self, **kw):
            calls.update(kw)

    monkeypatch.setattr(lf, "_get", lambda: _FakeLF())
    lf.record_generation(role="grader", model="qwen",
                         messages=[{"role": "user", "content": "x" * 20000}],
                         output="ok", latency_ms=123, error="")
    assert calls["name"] == "llm:grader" and calls["model"] == "qwen"
    assert len(calls["input"][0]["content"]) <= 8000     # payload capped
    assert calls["metadata"]["latency_ms"] == 123
