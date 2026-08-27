from __future__ import annotations

import subprocess

import pytest

from aiforge_core.runtime.tools import bash as bm


@pytest.fixture
def repo_root(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_REPO_ROOT", str(tmp_path))
    return tmp_path


# ─── error-shape consistency (audit fix) ───────────────────────────────


def test_error_returns_have_consistent_shape(repo_root, monkeypatch):
    # empty + delete-blocked errors must carry the same keys the success path
    # does (callers/tests read truncated/returncode without KeyError).
    for r in (bm.bash(""), bm.bash("rm -rf /tmp/x")):
        assert r["ok"] is False
        for k in ("command", "error", "returncode", "stdout", "stderr", "truncated"):
            assert k in r, (k, r)
    assert bm.bash("rm -rf /tmp/x").get("blocked") == "delete"


# ─── fallback path (no tmux) ────────────────────────────────────────────


def test_fallback_runs_simple_command(repo_root, monkeypatch):
    monkeypatch.setattr(bm, "_tmux_available", lambda: False)
    out = bm.bash("echo hi")
    assert out["ok"]
    assert out["returncode"] == 0
    assert out["stdout"].strip() == "hi"


def test_fallback_captures_nonzero_exit(repo_root, monkeypatch):
    monkeypatch.setattr(bm, "_tmux_available", lambda: False)
    out = bm.bash("false")
    assert out["ok"] is False
    assert out["returncode"] != 0


def test_fallback_timeout(repo_root, monkeypatch):
    monkeypatch.setattr(bm, "_tmux_available", lambda: False)
    out = bm.bash("sleep 5", timeout=1)
    assert out["ok"] is False
    assert out["error"] == "timeout"


def test_fallback_truncates_huge_stdout(repo_root, monkeypatch):
    import sys
    monkeypatch.setattr(bm, "_tmux_available", lambda: False)
    cmd = f"{sys.executable} -c \"print('x'*20000)\""
    out = bm.bash(cmd)
    assert out["ok"], out
    assert len(out["stdout"]) <= 8000
    assert out["truncated"] is True


def test_empty_command_rejected(repo_root):
    out = bm.bash("")
    assert out["ok"] is False
    assert out["error"] == "empty_command"


# ─── tmux path ──────────────────────────────────────────────────────────


pytestmark_tmux = pytest.mark.live_tmux


@pytestmark_tmux
def test_tmux_persists_cwd_across_calls(repo_root):
    run_id = "test-persist-cwd"
    try:
        bm.bash("mkdir -p sub && cd sub", _run_id=run_id)
        out = bm.bash("pwd", _run_id=run_id)
        assert out["ok"], out
        assert out["stdout"].strip().endswith("/sub")
    finally:
        bm.destroy_session(run_id)


@pytestmark_tmux
def test_tmux_persists_env_var(repo_root):
    run_id = "test-persist-env"
    try:
        bm.bash("export AIFORGE_TEST_VAR=ping", _run_id=run_id)
        out = bm.bash("echo $AIFORGE_TEST_VAR", _run_id=run_id)
        assert out["ok"], out
        assert out["stdout"].strip() == "ping"
    finally:
        bm.destroy_session(run_id)


@pytestmark_tmux
def test_tmux_restart_wipes_state(repo_root):
    run_id = "test-restart"
    try:
        bm.bash("export FOO=before", _run_id=run_id)
        bm.bash("true", restart=True, _run_id=run_id)
        out = bm.bash("echo ${FOO:-empty}", _run_id=run_id)
        assert out["stdout"].strip() == "empty"
    finally:
        bm.destroy_session(run_id)


@pytestmark_tmux
def test_tmux_destroy_session_cleans_up(repo_root):
    run_id = "test-destroy"
    bm.bash("true", _run_id=run_id)
    bm.destroy_session(run_id)
    proc = subprocess.run(
        ["tmux", "has-session", "-t", f"aiforge-{run_id}"],
        capture_output=True,
    )
    assert proc.returncode != 0


# ─── echoed-command stripping (no tmux needed) ──────────────────────────
#
# tmux types the command into a pty, so the pane echoes it back before the
# output. Without stripping, every tmux-path bash() returned
# "echo $FOO\nbar" where the stateless path returns "bar" — the tests below
# pin the parser itself, so a box with no tmux still guards the contract.


def test_strip_echoed_command_removes_the_echo():
    assert bm._strip_echoed_command("echo $FOO\nping", "echo $FOO") == "ping"


def test_strip_echoed_command_handles_a_wrapped_echo():
    """A long command wraps across pane-width lines; all of it is the echo."""
    cmd = "echo aaaaaaaaaa bbbbbbbbbb cccccccccc"
    body = "echo aaaaaaaaaa bbbbbb\nbbbb cccccccccc\nout"
    assert bm._strip_echoed_command(body, cmd) == "out"


def test_strip_echoed_command_keeps_a_body_that_is_not_an_echo():
    """No echo (or a mismatch) → not one byte of output is guessed away."""
    assert bm._strip_echoed_command("ping", "echo $FOO") == "ping"
    assert bm._strip_echoed_command("", "echo $FOO") == ""


def test_strip_echoed_command_keeps_output_identical_to_the_command():
    """`echo hi` printing `hi` twice keeps the second one."""
    assert bm._strip_echoed_command("echo hi\nhi", "echo hi") == "hi"


def test_strip_echoed_command_multiline_output_survives():
    assert bm._strip_echoed_command("ls\na\nb\nc", "ls") == "a\nb\nc"
