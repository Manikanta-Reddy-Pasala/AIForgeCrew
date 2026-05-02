"""Tests for the LLM router additions: cloud escalation + health probe."""
from __future__ import annotations

import os
from unittest import mock

import pytest

from aiforge_core.llm import router
from aiforge_core.llm import health
from aiforge_core.llm.types import Endpoint


# ── escalation ─────────────────────────────────────────────────────


def test_escalate_returns_none_when_under_threshold():
    """Token estimate well within local context → no escalation."""
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("AIFORGE_ESCALATE_DISABLE", None)
        assert router.escalate("doer", est_tokens=1_000) is None


def test_escalate_disabled_via_env():
    with mock.patch.dict(os.environ, {"AIFORGE_ESCALATE_DISABLE": "1"}):
        assert router.escalate("doer", est_tokens=1_000_000) is None


def test_escalate_skips_when_already_on_cloud():
    """If primary already cloud, escalate is a no-op."""
    fake_cloud_ep = Endpoint(
        base_url="https://api.anthropic.com", api_key="k",
        model="claude", provider="anthropic", role="doer", extras={},
    )
    with mock.patch.object(router, "resolve", return_value=fake_cloud_ep):
        assert router.escalate("doer", est_tokens=10_000_000) is None


# ── health probe ───────────────────────────────────────────────────


def test_health_disabled_returns_up():
    health.invalidate()
    with mock.patch.dict(os.environ, {"AIFORGE_HEALTH_DISABLE": "1"}):
        assert health.is_up("local", role="doer") is True


def test_health_caches_within_ttl(monkeypatch):
    health.invalidate()
    monkeypatch.delenv("AIFORGE_HEALTH_DISABLE", raising=False)
    monkeypatch.setenv("AIFORGE_HEALTH_TTL_S", "60")

    fake_state = health.HealthState(
        up=True, checked_at=health.time.time(), reason="http_200")
    health._CACHE["fake_provider"] = fake_state
    # Even though provider is unknown, the cached entry is honoured.
    assert health.is_up("fake_provider") is True


def test_health_unknown_provider_caches_down():
    health.invalidate()
    assert health.is_up("nope_xyz") is False
    # Second call should be cache hit
    assert "nope_xyz" in health._CACHE
    assert health._CACHE["nope_xyz"].up is False


def test_snapshot_returns_cache_view():
    health.invalidate()
    health._CACHE["x"] = health.HealthState(
        up=True, checked_at=health.time.time(), reason="ok")
    snap = health.snapshot()
    assert snap["x"]["up"] is True
    assert "age_s" in snap["x"]


def test_invalidate_clears_all_when_no_arg():
    health._CACHE["a"] = health.HealthState(up=True, checked_at=0.0)
    health._CACHE["b"] = health.HealthState(up=False, checked_at=0.0)
    health.invalidate()
    assert health._CACHE == {}


def test_invalidate_clears_one_when_arg_passed():
    health._CACHE["a"] = health.HealthState(up=True, checked_at=0.0)
    health._CACHE["b"] = health.HealthState(up=False, checked_at=0.0)
    health.invalidate("a")
    assert "a" not in health._CACHE
    assert "b" in health._CACHE
    health.invalidate()


# ── client.py garbage detection ───────────────────────────────────


def test_is_garbage_recognises_empty():
    from aiforge_core.llm.client import _is_garbage
    assert _is_garbage("") is True
    assert _is_garbage("   ") is True
    assert _is_garbage("ok") is True  # too short


def test_is_garbage_recognises_stop_token_leak():
    from aiforge_core.llm.client import _is_garbage
    assert _is_garbage("<|im_end|>") is True
    assert _is_garbage("<tool_call>") is True


def test_is_garbage_passes_real_text():
    from aiforge_core.llm.client import _is_garbage
    assert _is_garbage("Here is the answer.") is False
    assert _is_garbage('{"verdict": "pass"}') is False
