"""The janitor that reclaims finished tickets' worktrees.

It deletes directories, so the rules are written to fail toward KEEPING work:
only a ticket that will never be worked again (done / cancelled / qa_failed)
loses its worktree, an unknown ticket is kept, and a store that cannot be read
keeps everything.

``blocked`` is deliberately NOT terminal. The shell version this replaces
treated it as terminal and deleted the worktree of a ticket an operator was
about to unblock — throwing away work in progress. That version also queried
Postgres, which this SQLite-only build does not have: it demanded a password,
exited 1 without it, and would have read an empty status for every ticket
anyway. Status now comes from the ticket store, so it follows the backend.
"""
from __future__ import annotations

import types as pytypes

import pytest

from scripts.runtime import worktree_janitor as J


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    """Two repos, each with a worktree per ticket."""
    def _mk(repo: str, *tickets: str):
        d = tmp_path / repo / ".aiforge-worktrees"
        d.mkdir(parents=True)
        for t in tickets:
            (d / t).mkdir()
        return d
    _mk("app", "ONE-1", "ONE-2")
    _mk("lib", "ONE-3")
    (tmp_path / "not-a-repo").mkdir()
    monkeypatch.setenv("AIFORGE_WORKTREE_ROOT", str(tmp_path))
    return tmp_path


@pytest.fixture()
def store(monkeypatch):
    """Ticket statuses, and a git that reports what it was asked to do."""
    from aiforge_core.tickets import store as st
    state: dict = {"status": {}, "git": [], "rc": 0, "raise": None}
    monkeypatch.setattr(
        st, "get",
        lambda ident: (_ for _ in ()).throw(state["raise"]) if state["raise"]
        else (pytypes.SimpleNamespace(status=state["status"][ident])
              if ident in state["status"] else None))

    def _run(argv, cwd=None, text=None, capture_output=None, timeout=None):
        state["git"].append((cwd, list(argv)))
        return pytypes.SimpleNamespace(returncode=state["rc"], stdout="",
                                       stderr="fatal: not a working tree")
    monkeypatch.setattr(J.subprocess, "run", _run)
    return state


def _removed(state):
    return {argv[-1].rsplit("/", 1)[-1] for _cwd, argv in state["git"]
            if argv[1:3] == ["worktree", "remove"]}


# ─── what gets reclaimed ───────────────────────────────────────────────


@pytest.mark.parametrize("status", ["done", "cancelled", "qa_failed"])
def test_a_finished_tickets_worktree_is_reclaimed(workspace, store, status):
    store["status"] = {"ONE-1": status}
    assert J.main([]) == 0
    assert "ONE-1" in _removed(store)


@pytest.mark.parametrize("status", ["todo", "in_progress", "in_review", "qa"])
def test_an_active_tickets_worktree_is_left_alone(workspace, store, status):
    store["status"] = {"ONE-1": status}
    J.main([])
    assert _removed(store) == set()


def test_a_blocked_ticket_keeps_its_worktree(workspace, store):
    """An operator unblocks it and resumes — the old script deleted the work."""
    store["status"] = {"ONE-1": "blocked"}
    J.main([])
    assert _removed(store) == set()


def test_a_ticket_nobody_has_heard_of_is_kept(workspace, store):
    store["status"] = {}
    J.main([])
    assert _removed(store) == set()


def test_a_store_that_cannot_be_read_keeps_everything(workspace, store):
    store["raise"] = OSError("db locked")
    assert J.main([]) == 0
    assert _removed(store) == set()


def test_every_repo_under_the_root_is_swept(workspace, store):
    store["status"] = {"ONE-1": "done", "ONE-3": "cancelled",
                       "ONE-2": "in_progress"}
    J.main([])
    assert _removed(store) == {"ONE-1", "ONE-3"}


def test_each_removal_runs_in_its_own_repo(workspace, store):
    store["status"] = {"ONE-3": "done"}
    J.main([])
    cwd = next(c for c, argv in store["git"] if argv[1:3] == ["worktree",
                                                              "remove"])
    assert cwd.endswith("/lib")


def test_stale_refs_are_pruned_once_per_repo(workspace, store):
    store["status"] = {"ONE-1": "done"}
    J.main([])
    prunes = [c for c, argv in store["git"] if argv[1:] == ["worktree",
                                                            "prune"]]
    assert sorted(p.rsplit("/", 1)[-1] for p in prunes) == ["app", "lib"]


# ─── reporting and safety ──────────────────────────────────────────────


def test_a_dry_run_touches_nothing(workspace, store, capsys):
    store["status"] = {"ONE-1": "done"}
    assert J.main(["--dry-run"]) == 0
    assert store["git"] == [], "no remove, and no prune either"
    assert "WOULD remove" in capsys.readouterr().out


def test_a_failed_removal_is_reported_as_a_failure(workspace, store):
    store["status"] = {"ONE-1": "done"}
    store["rc"] = 1
    assert J.main([]) == 1, "systemd should mark the unit failed"


def test_a_sweep_with_nothing_to_do_is_a_success(workspace, store):
    assert J.main([]) == 0


def test_a_git_that_will_not_run_is_survived(workspace, store, monkeypatch):
    store["status"] = {"ONE-1": "done"}
    monkeypatch.setattr(J.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no git")))
    assert J.main([]) == 1


def test_a_root_that_is_not_there_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_WORKTREE_ROOT", str(tmp_path / "nope"))
    assert J.main([]) == 0


def test_an_unreadable_root_is_reported_not_raised(tmp_path, monkeypatch,
                                                   capsys):
    monkeypatch.setattr(J.Path, "iterdir",
                        lambda self: (_ for _ in ()).throw(OSError("perm")))
    monkeypatch.setenv("AIFORGE_WORKTREE_ROOT", str(tmp_path))
    assert J.main([]) == 0
    assert "cannot read" in capsys.readouterr().out


def test_the_root_comes_from_the_env_or_the_convention(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_WORKTREE_ROOT", str(tmp_path))
    assert J._root() == tmp_path
    monkeypatch.delenv("AIFORGE_WORKTREE_ROOT", raising=False)
    assert J._root().name == "codeRepo"


def test_an_explicit_root_wins_over_the_env(workspace, store, tmp_path,
                                            monkeypatch):
    other = tmp_path / "other"
    (other / "app" / ".aiforge-worktrees" / "ONE-9").mkdir(parents=True)
    store["status"] = {"ONE-9": "done", "ONE-1": "done"}
    J.main(["--root", str(other)])
    assert _removed(store) == {"ONE-9"}


def test_a_repo_with_no_worktrees_is_skipped(workspace, store):
    J.main([])
    assert not any("not-a-repo" in (c or "") for c, _ in store["git"])
