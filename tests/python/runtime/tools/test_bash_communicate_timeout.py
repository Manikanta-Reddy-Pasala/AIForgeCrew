"""bash fallback: a daemon grandchild inheriting the stdout pipe (``npm run
dev &``) keeps ``communicate()`` blocked forever even after the main process
exits. The fallback must bound ``communicate()`` and, on timeout, kill the
process group + drain, returning what was captured instead of hanging."""
import subprocess

import pytest


class _FakeProc:
    """Main process already exited (poll()->0); communicate() with NO timeout
    would block forever (simulating the pipe held by a daemon grandchild). A
    BOUNDED communicate() raises TimeoutExpired the first time; the post-kill
    drain returns captured bytes."""

    def __init__(self):
        self.pid = 4242
        self.returncode = 0
        self._calls = 0

    def poll(self):
        return 0   # already exited — the poll loop falls straight through

    def communicate(self, timeout=None):
        if timeout is None:
            # The OLD unbounded call path: returns without ever triggering a
            # kill (in reality it would hang; here it simply never kills).
            return (b"unbounded", b"")
        self._calls += 1
        if self._calls == 1:
            raise subprocess.TimeoutExpired(cmd="x", timeout=timeout)
        return (b"drained-output", b"")


@pytest.fixture
def bash(monkeypatch):
    from aiforge_core.runtime.tools import bash as b
    from aiforge_core.runtime import chat_cancel

    # Force the cancellable fallback branch (needs an active chat session).
    monkeypatch.setattr(chat_cancel, "active", lambda: "sid-1")
    monkeypatch.setattr(chat_cancel, "is_cancelled", lambda _s: False)
    monkeypatch.setattr(chat_cancel, "track_pgid", lambda *a, **k: None)
    monkeypatch.setattr(b.subprocess, "Popen", lambda *a, **k: _FakeProc())
    monkeypatch.setattr(b.os, "getpgid", lambda _pid: 4242)
    return b


def test_communicate_timeout_kills_group_and_returns(bash, monkeypatch):
    killed = []
    monkeypatch.setattr(bash.os, "killpg", lambda pgid, sig: killed.append((pgid, sig)))

    res = bash._fallback_run("npm run dev &", timeout=5)

    # The group was killed (RED on the old unbounded communicate() path) ...
    assert killed, "expected the process group to be killed on communicate timeout"
    # ... and we still returned the drained output instead of hanging.
    assert res["ok"] is True
    assert "drained-output" in res["stdout"]


def test_normal_fast_command_unaffected(bash, monkeypatch):
    """A proc that returns promptly from the bounded communicate() must not
    trigger the kill path."""
    class _FastProc(_FakeProc):
        def communicate(self, timeout=None):
            return (b"hello", b"")

    monkeypatch.setattr(bash.subprocess, "Popen", lambda *a, **k: _FastProc())
    killed = []
    monkeypatch.setattr(bash.os, "killpg", lambda pgid, sig: killed.append(1))

    res = bash._fallback_run("echo hello", timeout=5)
    assert res["ok"] is True
    assert "hello" in res["stdout"]
    assert not killed
