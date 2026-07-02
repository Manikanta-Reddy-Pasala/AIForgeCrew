"""Pipeline (ADK Doer) fires the same PreToolUse/PostToolUse hooks as chat."""
from __future__ import annotations

import asyncio
import json

import pytest

from aiforge_core.runtime import hooks


class _Tool:
    name = "run_command"


def _write_hooks(tmp_path, cfg):
    (tmp_path / "hooks.json").write_text(json.dumps(cfg))


def test_before_adapter_blocks_on_veto(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("AIFORGE_HOOKS_DISABLE", raising=False)
    _write_hooks(tmp_path, {"PreToolUse": [
        {"matcher": "run_command", "command": "exit 1",
         "block_on_nonzero": True}]})
    cb = hooks.adk_before_tool_callback()
    out = asyncio.run(cb(tool=_Tool(), args={"command": "ls"}))
    assert isinstance(out, dict) and out.get("blocked") == "hook"


def test_before_adapter_allows_when_no_block(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("AIFORGE_HOOKS_DISABLE", raising=False)
    _write_hooks(tmp_path, {"PreToolUse": [
        {"matcher": "run_command", "command": "exit 0"}]})
    cb = hooks.adk_before_tool_callback()
    out = asyncio.run(cb(tool=_Tool(), args={"command": "ls"}))
    assert out is None


def test_after_adapter_runs_hook(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("AIFORGE_HOOKS_DISABLE", raising=False)
    marker = tmp_path / "ran.txt"
    _write_hooks(tmp_path, {"PostToolUse": [
        {"matcher": "*", "command": f"touch {marker}"}]})
    cb = hooks.adk_after_tool_callback()

    async def _run():
        await cb(tool=_Tool(), args={}, tool_response={"ok": True})
        # after-hook is fire-and-forget in an executor; give it a beat
        await asyncio.sleep(0.5)
    asyncio.run(_run())
    assert marker.exists()


def test_adapters_noop_when_disabled(monkeypatch):
    monkeypatch.setenv("AIFORGE_HOOKS_DISABLE", "1")
    assert hooks.adk_before_tool_callback() is None
    assert hooks.adk_after_tool_callback() is None
