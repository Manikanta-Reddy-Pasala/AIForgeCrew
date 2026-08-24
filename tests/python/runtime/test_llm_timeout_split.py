"""EscalatingLlm connect/read timeout split.

A hanging (asleep / firewalled, no TCP RST) LLM endpoint must fail the
CONNECT fast so escalation moves on, instead of blocking the full read
timeout (600s) × retries × chain — the "pipeline runs forever" symptom.
litellm forwards httpx.Timeout natively, so the fix is a split timeout.
"""
from __future__ import annotations

import httpx
import pytest


@pytest.fixture
def _capture_litellm(monkeypatch):
    import google.adk.models.lite_llm as ll
    seen: dict = {}

    class _Fake(ll.LiteLlm):
        def __init__(self, **kw):  # noqa: D401 — capture, skip real init
            seen.update(kw)

    monkeypatch.setattr(ll, "LiteLlm", _Fake)
    return seen


def test_timeout_is_split_connect_short_read_long(_capture_litellm, monkeypatch):
    monkeypatch.setenv("AIFORGE_LLM_CONNECT_TIMEOUT_S", "8")
    monkeypatch.setenv("AIFORGE_LLM_TIMEOUT_S", "600")
    from aiforge_core.runtime import escalating_llm as e
    e._build_one({"model_id": "openai/x", "api_base": "http://127.0.0.1:9"})
    t = _capture_litellm["timeout"]
    assert isinstance(t, httpx.Timeout)
    assert t.connect == 8.0        # fails an unreachable host in seconds
    assert t.read == 600.0         # still lets a live model think for minutes


def test_connect_never_exceeds_read(_capture_litellm, monkeypatch):
    # A misconfigured connect > read must be clamped to read (never wait
    # longer to connect than the whole read budget).
    monkeypatch.setenv("AIFORGE_LLM_CONNECT_TIMEOUT_S", "999")
    monkeypatch.setenv("AIFORGE_LLM_TIMEOUT_S", "30")
    from aiforge_core.runtime import escalating_llm as e
    e._build_one({"model_id": "openai/x", "api_base": "http://127.0.0.1:9"})
    t = _capture_litellm["timeout"]
    assert t.connect == 30.0
    assert t.read == 30.0


def test_bad_env_falls_back_to_defaults(_capture_litellm, monkeypatch):
    monkeypatch.setenv("AIFORGE_LLM_CONNECT_TIMEOUT_S", "not-a-number")
    monkeypatch.setenv("AIFORGE_LLM_TIMEOUT_S", "also-bad")
    from aiforge_core.runtime import escalating_llm as e
    e._build_one({"model_id": "openai/x", "api_base": "http://127.0.0.1:9"})
    t = _capture_litellm["timeout"]
    assert t.connect == 8.0
    assert t.read == 900.0
