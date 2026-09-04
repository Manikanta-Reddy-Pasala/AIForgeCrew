from __future__ import annotations

from unittest.mock import AsyncMock, patch

from aiforge_core.runtime import request_context as rc
from aiforge_core.runtime.tools import delegation as dlg


def test_unknown_role_soft_errors():
    out = dlg.delegate_to_agent("totally-fake", "do something")
    assert out["ok"] is False
    assert out["error"] == "unknown_role"
    assert "researcher" in out["allowed"]


def test_empty_prompt_rejected():
    out = dlg.delegate_to_agent("researcher", "")
    assert out["ok"] is False
    assert out["error"] == "empty_prompt"


def test_delegate_happy(monkeypatch):
    async def _fake_run(role, prompt):
        return {
            "ok": True, "role": role,
            "output": "research notes ...", "state_keys": ["research_brief"],
        }
    monkeypatch.setattr(dlg, "_run_delegate_async", _fake_run)
    out = dlg.delegate_to_agent("researcher", "context for ONE-200")
    assert out["ok"]
    assert out["role"] == "researcher"
    assert "research notes" in out["output"]
    assert out["state_keys"] == ["research_brief"]
    assert out["wall_s"] >= 0


def test_delegate_propagates_exception_as_soft_error(monkeypatch):
    async def _boom(role, prompt):
        raise RuntimeError("ADK launch failed")
    monkeypatch.setattr(dlg, "_run_delegate_async", _boom)
    out = dlg.delegate_to_agent("planner", "plan something")
    assert out["ok"] is False
    assert out["error"] == "delegate_failed"
    assert "ADK launch failed" in out["detail"]


def test_delegate_timeout(monkeypatch):
    """The deadline lives at the boundary now, so this drives the real one:
    a delegate that outlives it, cancelled by asyncio.timeout()."""
    import asyncio

    async def _slow(role, prompt):
        await asyncio.sleep(5)
        return {"ok": True, "role": role, "output": "too late",
                "state_keys": []}
    monkeypatch.setattr(dlg, "_run_delegate_async", _slow)
    out = dlg.delegate_to_agent("verifier", "verify x", timeout=1)
    assert out["ok"] is False
    assert out["error"] == "timeout"


def test_delegation_depth_cap(monkeypatch):
    # Depth is now request-scoped (contextvar), not process-global env — so
    # concurrent chains can't clobber each other. Drive it up to the cap.
    async def _ok(role, prompt):
        return {"ok": True, "role": role, "output": "x", "state_keys": []}
    monkeypatch.setattr(dlg, "_run_delegate_async", _ok)
    monkeypatch.setenv("AIFORGE_DELEGATION_MAX_DEPTH", "3")
    toks = [rc.enter_delegation() for _ in range(3)]  # depth == 3 == max
    try:
        out = dlg.delegate_to_agent("researcher", "do stuff")
    finally:
        for t in reversed(toks):
            rc.reset_delegation(t)
    assert out["ok"] is False
    assert out["error"] == "delegation_depth_exceeded"
    assert out["max_depth"] == 3


def test_delegation_depth_increments(monkeypatch):
    captured = {}
    async def _capture(role, prompt):
        captured["depth_inside"] = rc.get_delegation_depth()
        return {"ok": True, "role": role, "output": "", "state_keys": []}
    monkeypatch.setattr(dlg, "_run_delegate_async", _capture)
    tok = rc.enter_delegation()  # start at depth 1
    try:
        out = dlg.delegate_to_agent("planner", "plan")
    finally:
        rc.reset_delegation(tok)
    assert out["ok"]
    assert captured["depth_inside"] == 2  # incremented before async run
    assert out["depth"] == 1              # outer call recorded its depth
