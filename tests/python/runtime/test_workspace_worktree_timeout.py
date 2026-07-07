"""``git worktree add`` had no timeout (the sibling fetch does): a stale index
lock or hung FS would hang the runner with the ticket already in_progress. The
add must be bounded and return None cleanly on TimeoutExpired."""
import os
import subprocess
import types

import pytest


class _Ticket:
    def __init__(self, repo_dir_name):
        self.project = repo_dir_name
        self.parent_id = None
        self.title = "do a thing"
        self.body = ""
        self.branch = None
        self.identifier = "ONE-100"
        self.id = 1


@pytest.fixture
def ws(monkeypatch, tmp_path):
    from aiforge_core.runtime import workspace as w
    # A fake base folder containing a "repo" with a .git dir. The base is read
    # via repo_map.default_root(), which honours AIFORGE_WORKTREE_ROOT when no
    # stored default_root is set — point both at an isolated tmp dir.
    root = tmp_path / "codeRepo"
    repo = root / "MyRepo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setenv("AIFORGE_WORKTREE_ROOT", str(root))
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    return w, str(repo)


def _fake_run_factory(on_worktree_add):
    def _run(cmd, *a, **k):
        if "worktree" in cmd and "add" in cmd:
            return on_worktree_add(cmd, *a, **k)
        # fetch / rev-parse / symbolic-ref → succeed
        cp = types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        return cp
    return _run


def test_worktree_add_timeout_returns_none(ws, monkeypatch):
    w, repo_dir = ws

    def _raise_timeout(cmd, *a, **k):
        # Assert the fix actually passes a timeout to the add.
        assert "timeout" in k and k["timeout"], "worktree add must have a timeout"
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=k["timeout"])

    monkeypatch.setattr(w.subprocess, "run",
                        _fake_run_factory(_raise_timeout))

    # Must not hang / raise — returns None so the caller blocks the ticket.
    assert w.ensure_branch_and_worktree(_Ticket("MyRepo")) is None


def test_worktree_add_success_path_unaffected(ws, monkeypatch):
    w, repo_dir = ws
    made = {}

    def _ok(cmd, *a, **k):
        # Create the worktree dir so the returncode/isdir check passes.
        wt = cmd[cmd.index("add") + 3]  # ["git","worktree","add","-B",br,PATH,base]
        os.makedirs(wt, exist_ok=True)
        made["path"] = wt
        return types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(w.subprocess, "run", _fake_run_factory(_ok))
    # branch persist writes to tickets._conn (Postgres) — stub it out.
    monkeypatch.setattr(w.tickets, "_conn",
                        lambda: (_ for _ in ()).throw(RuntimeError("no pg")),
                        raising=False)

    out = w.ensure_branch_and_worktree(_Ticket("MyRepo"))
    assert out == made["path"]
