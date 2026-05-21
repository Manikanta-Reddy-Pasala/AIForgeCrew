from __future__ import annotations

from unittest.mock import AsyncMock, patch

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
    async def _fake_run(role, prompt, timeout):
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
    async def _boom(role, prompt, timeout):
        raise RuntimeError("ADK launch failed")
    monkeypatch.setattr(dlg, "_run_delegate_async", _boom)
    out = dlg.delegate_to_agent("planner", "plan something")
    assert out["ok"] is False
    assert out["error"] == "delegate_failed"
    assert "ADK launch failed" in out["detail"]


def test_delegate_timeout(monkeypatch):
    async def _slow(role, prompt, timeout):
        return {"ok": False, "error": "timeout", "role": role}
    monkeypatch.setattr(dlg, "_run_delegate_async", _slow)
    out = dlg.delegate_to_agent("verifier", "verify x", timeout=1)
    assert out["ok"] is False
    assert out["error"] == "timeout"
