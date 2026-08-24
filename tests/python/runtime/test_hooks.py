"""Tests for the lifecycle hooks system (Claude Code parity, LOCAL-only).

Covers: matching-hook execution, matcher filtering, PreToolUse
block_on_nonzero gating, the soft-fail no-ops (missing / malformed config,
kill switch), and the subprocess timeout guard.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from aiforge_core.config import _filecache
from aiforge_core.runtime import hooks


def _write_hooks(cfg_dir: Path, data) -> None:
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "hooks.json").write_text(
        data if isinstance(data, str) else json.dumps(data))
    _filecache.clear()   # defeat the mtime cache within a single test tick


@pytest.fixture(autouse=True)
def _clear_cache():
    _filecache.clear()
    yield
    _filecache.clear()


# ─── (a) a matching hook actually runs ────────────────────────────────

def test_posttooluse_runs_matching_hook(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    marker = tmp_path / "ran.txt"
    _write_hooks(tmp_path, {
        "PostToolUse": [{"matcher": "*", "command": f"echo ok > {marker}"}],
    })
    out = hooks.fire("PostToolUse", {"tool": "file_write", "args": {}},
                     str(tmp_path))
    assert out["ok"] is True
    assert marker.exists()
    assert marker.read_text().strip() == "ok"


# ─── (b) matcher filtering ────────────────────────────────────────────

def test_matcher_filters_non_matching_tool(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    marker = tmp_path / "should_not.txt"
    _write_hooks(tmp_path, {
        "PostToolUse": [{"matcher": "run_command",
                         "command": f"echo x > {marker}"}],
    })
    # fire for a DIFFERENT tool → hook must not run
    out = hooks.fire("PostToolUse", {"tool": "file_write", "args": {}},
                     str(tmp_path))
    assert out["ok"] is True
    assert out["results"] == []
    assert not marker.exists()

    # fire for the matching tool → hook runs
    hooks.fire("PostToolUse", {"tool": "run_command", "args": {}}, str(tmp_path))
    assert marker.exists()


def test_matcher_alternation(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    _write_hooks(tmp_path, {
        "PostToolUse": [{"matcher": "run_command|file_write", "command": "true"}],
    })
    assert hooks.fire("PostToolUse", {"tool": "file_write"}, str(tmp_path))["results"]
    _filecache.clear()
    assert hooks.fire("PostToolUse", {"tool": "grep"}, str(tmp_path))["results"] == []


# ─── (c) PreToolUse block_on_nonzero ──────────────────────────────────

def test_pretooluse_block_on_nonzero(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    _write_hooks(tmp_path, {
        "PreToolUse": [{"matcher": "*", "command": "exit 1",
                        "block_on_nonzero": True}],
    })
    out = hooks.fire("PreToolUse", {"tool": "run_command"}, str(tmp_path))
    assert out["blocked"] is True


def test_pretooluse_no_block_on_zero(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    _write_hooks(tmp_path, {
        "PreToolUse": [{"matcher": "*", "command": "exit 0",
                        "block_on_nonzero": True}],
    })
    out = hooks.fire("PreToolUse", {"tool": "run_command"}, str(tmp_path))
    assert out["blocked"] is False


def test_pretooluse_nonzero_without_flag_does_not_block(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    _write_hooks(tmp_path, {
        "PreToolUse": [{"matcher": "*", "command": "exit 3"}],   # no flag
    })
    out = hooks.fire("PreToolUse", {"tool": "run_command"}, str(tmp_path))
    assert out["blocked"] is False


# ─── (d) no hooks.json → no-op ────────────────────────────────────────

def test_missing_config_is_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))   # no hooks.json
    out = hooks.fire("PostToolUse", {"tool": "file_write"}, str(tmp_path))
    assert out == {"ok": True, "blocked": False, "results": []}


# ─── (e) kill switch ──────────────────────────────────────────────────

def test_kill_switch_is_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_HOOKS_DISABLE", "1")
    marker = tmp_path / "nope.txt"
    _write_hooks(tmp_path, {
        "PostToolUse": [{"matcher": "*", "command": f"echo x > {marker}"}],
    })
    out = hooks.fire("PostToolUse", {"tool": "file_write"}, str(tmp_path))
    assert out == {"ok": True, "blocked": False, "results": []}
    assert not marker.exists()


# ─── (f) malformed config → soft-fail no-op ───────────────────────────

def test_malformed_config_soft_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    _write_hooks(tmp_path, "{ this is not valid json ")
    out = hooks.fire("PostToolUse", {"tool": "file_write"}, str(tmp_path))
    assert out == {"ok": True, "blocked": False, "results": []}


# ─── (g) timeout must not hang ─────────────────────────────────────────

def test_hook_timeout_does_not_hang(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_HOOK_TIMEOUT_S", "1")
    _write_hooks(tmp_path, {
        "PostToolUse": [{"matcher": "*", "command": "sleep 30"}],
    })
    import time as _t
    t0 = _t.monotonic()
    out = hooks.fire("PostToolUse", {"tool": "file_write"}, str(tmp_path))
    elapsed = _t.monotonic() - t0
    assert elapsed < 10                 # returned promptly, did not hang
    assert out["ok"] is True            # soft-fail
    assert out["blocked"] is False


# ─── repo-local merges over global ────────────────────────────────────

def test_repo_local_hooks_merge(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg"
    repo = tmp_path / "repo"
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(cfg))
    g_marker = tmp_path / "global.txt"
    r_marker = tmp_path / "repo.txt"
    _write_hooks(cfg, {
        "PostToolUse": [{"matcher": "*", "command": f"echo g > {g_marker}"}],
    })
    _write_hooks(repo / ".aiforge", {
        "PostToolUse": [{"matcher": "*", "command": f"echo r > {r_marker}"}],
    })
    out = hooks.fire("PostToolUse", {"tool": "file_write"}, str(repo))
    assert out["ok"] is True
    assert g_marker.exists()
    assert r_marker.exists()


# ─── chat-loop integration: a PreToolUse block skips the tool ─────────

def test_chat_pretooluse_block_skips_tool(tmp_path, monkeypatch):
    """A repo-local PreToolUse hook that blocks `run_command` must stop the
    tool from running inside run_chat_agent — the side effect never happens
    and a `blocked: "hook"` result surfaces."""
    from aiforge_core.runtime import chat_agent as ca, chat_cancel

    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    marker = tmp_path / "side_effect.txt"
    # repo-local hooks.json blocks run_command
    _write_hooks(tmp_path / ".aiforge", {
        "PreToolUse": [{"matcher": "run_command", "command": "exit 1",
                        "block_on_nonzero": True}],
    })

    def _fn(role, messages, **kw):
        return _fn.seq.pop(0)
    _fn.seq = [
        f'ACTION: run_command\nARGS_JSON: {{"cmd": "echo x > {marker}"}}',
        "FINAL: done",
    ]

    evs = list(ca.run_chat_agent(
        [{"role": "user", "content": "run it"}], cwd=str(tmp_path),
        complete_fn=_fn))
    chat_cancel.set_active(None)

    tool = [e for e in evs if e["type"] == "tool"][0]
    assert tool["result"].get("blocked") == "hook"
    assert not marker.exists()          # the command never ran
