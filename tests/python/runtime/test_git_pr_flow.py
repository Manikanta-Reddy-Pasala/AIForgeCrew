"""Commit → push → open PR, and the guards along the way.

Three of these guards were each a real incident. A ticket re-run leaves a
diverged branch on origin, so a push rejected as non-fast-forward retries with
``--force-with-lease`` — but only inside the ``aiforge/*`` namespace, never on
a shared branch. A second ``gh pr create`` for a branch that already has an
open PR is not a failure: the work is shipped, so the existing URL is
recovered rather than dead-ending the ticket as blocked. And a diff of ONLY
test files is rejected: tests prove a fix, they are not the fix, and ONE-3
published a no-op PR that way.

Every git/gh invocation is stubbed.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import types

import pytest

from aiforge_core.runtime.git_pr import _pr


@pytest.fixture
def git(monkeypatch):
    """Record git/gh argv; queue per-command replies."""
    state: dict = {"calls": [], "replies": {}, "default": (0, "", "")}

    def _run_git(argv, repo_root=None, **kw):
        state["calls"].append(argv)
        for key, rep in state["replies"].items():
            if key in " ".join(argv):
                return rep(state) if callable(rep) else rep
        return state["default"]
    monkeypatch.setattr(_pr, "run_git", _run_git)
    monkeypatch.setattr(_pr, "_EXCLUDE_PATHSPECS", [":(exclude).aiforge"])
    return state


def _ticket(**kw):
    base = {"identifier": "ONE-1", "title": "Fix the parser", "body": "details",
            "branch": None, "id": 1, "project": ""}
    base.update(kw)
    return types.SimpleNamespace(**base)


# ─── is there anything to ship ─────────────────────────────────────────


def test_uncommitted_work_counts_as_changes(git):
    git["replies"]["status"] = (0, " M app/store.py\n", "")
    assert _pr._has_doer_changes("/repo") == (True, "")


def test_commits_the_doer_made_in_loop_also_count(git, monkeypatch):
    git["replies"]["status"] = (0, "", "")
    monkeypatch.setattr(_pr, "_has_unpushed_commits", lambda root: (True, "origin/main"))
    assert _pr._has_doer_changes("/repo") == (True, "")


def test_a_clean_tree_with_nothing_ahead_has_no_changes(git, monkeypatch):
    git["replies"]["status"] = (0, "", "")
    monkeypatch.setattr(_pr, "_has_unpushed_commits", lambda root: (False, ""))
    assert _pr._has_doer_changes("/repo") == (False, "no_changes")


def test_a_broken_git_status_is_reported(git):
    git["replies"]["status"] = (128, "", "not a repository")
    assert _pr._has_doer_changes("/repo") == (False, "git_status_failed")


# ─── branching ─────────────────────────────────────────────────────────


def test_the_branch_is_force_reset_to_head(git):
    """`checkout -B` both creates and resets — the old delete+create pair
    failed when HEAD was already on the branch, which bricked a retry run."""
    assert _pr._checkout_branch("/repo", "aiforge/ONE-1") == ""
    assert git["calls"][0] == ["git", "checkout", "-B", "aiforge/ONE-1"]


def test_a_failed_checkout_is_a_skip_reason(git):
    git["replies"]["checkout"] = (1, "", "cannot lock ref")
    assert _pr._checkout_branch("/repo", "b") == "checkout_failed"


# ─── the gitignore ─────────────────────────────────────────────────────


def test_a_default_gitignore_is_written_when_absent(tmp_path):
    _pr._ensure_gitignore(str(tmp_path))
    assert "AIForgeCrew" in (tmp_path / ".gitignore").read_text()


def test_an_existing_gitignore_is_never_touched(tmp_path):
    (tmp_path / ".gitignore").write_text("mine\n")
    _pr._ensure_gitignore(str(tmp_path))
    assert (tmp_path / ".gitignore").read_text() == "mine\n"


def test_an_unwritable_gitignore_is_not_fatal(tmp_path, monkeypatch):
    monkeypatch.setattr("builtins.open",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")))
    _pr._ensure_gitignore(str(tmp_path))


# ─── committing ────────────────────────────────────────────────────────


def test_everything_in_the_worktree_is_staged_with_artifacts_excluded(git):
    _pr._stage_doer_changes("/repo")
    assert git["calls"][0][:4] == ["git", "add", "-A", "--"]
    assert ":(exclude).aiforge" in git["calls"][0]


def test_a_commit_carries_the_ticket_and_title(git, monkeypatch, tmp_path):
    monkeypatch.setattr(_pr, "_ensure_gitignore", lambda root: None)
    assert _pr._commit_changes(str(tmp_path), "ONE-1", "Fix the parser") == ""
    msg = next(c for c in git["calls"] if c[1] == "commit")[3]
    assert msg.startswith("feat(ONE-1): Fix the parser")


def test_nothing_to_commit_is_not_a_failure(git, monkeypatch, tmp_path):
    monkeypatch.setattr(_pr, "_ensure_gitignore", lambda root: None)
    git["replies"]["commit"] = (1, "nothing to commit, working tree clean", "")
    assert _pr._commit_changes(str(tmp_path), "ONE-1", "t") == ""


def test_a_real_commit_failure_is_a_skip_reason(git, monkeypatch, tmp_path):
    monkeypatch.setattr(_pr, "_ensure_gitignore", lambda root: None)
    git["replies"]["commit"] = (1, "", "pre-commit hook failed")
    assert _pr._commit_changes(str(tmp_path), "ONE-1", "t") == "commit_failed"


# ─── the remote probe ──────────────────────────────────────────────────


def test_no_origin_is_a_clean_reason(git):
    git["replies"]["remote"] = (0, "upstream\n", "")
    assert _pr._has_reachable_remote("/repo") == (False, "no_origin_configured")


def test_a_reachable_origin_passes(git, monkeypatch):
    git["replies"]["remote"] = (0, "origin\n", "")
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(returncode=0))
    assert _pr._has_reachable_remote("/repo") == (True, "")


def test_a_slow_remote_is_bounded(git, monkeypatch):
    """Catching this before `git push` saves ~30s on every sandbox ticket."""
    git["replies"]["remote"] = (0, "origin\n", "")
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(
                            subprocess.TimeoutExpired(cmd="ls-remote", timeout=10)))
    assert _pr._has_reachable_remote("/repo") == (False, "remote_unreachable_timeout")


def test_an_unreachable_remote_is_reported(git, monkeypatch):
    git["replies"]["remote"] = (0, "origin\n", "")
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(returncode=128))
    assert _pr._has_reachable_remote("/repo") == (False, "remote_unreachable")


def test_a_probe_error_is_reported(git, monkeypatch):
    git["replies"]["remote"] = (0, "origin\n", "")
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no git")))
    ok, reason = _pr._has_reachable_remote("/repo")
    assert ok is False
    assert reason.startswith("remote_probe_error")


# ─── pushing ───────────────────────────────────────────────────────────


@pytest.fixture
def reachable(monkeypatch):
    monkeypatch.setattr(_pr, "_has_reachable_remote", lambda root: (True, ""))


def test_a_clean_push(git, reachable):
    assert _pr._push("/repo", "aiforge/ONE-1") == (True, "")


def test_an_unreachable_remote_skips_the_push(git, monkeypatch):
    monkeypatch.setattr(_pr, "_has_reachable_remote",
                        lambda root: (False, "no_origin_configured"))
    assert _pr._push("/repo", "b") == (False, "no_origin_configured")
    assert git["calls"] == []


@pytest.mark.parametrize("err", ["non-fast-forward", "updates were rejected",
                                 "fetch first", "stale info"])
def test_a_diverged_ticket_branch_retries_with_a_lease(git, reachable, err):
    """One ticket = one aiforge/* branch, so a lease-guarded force is safe."""
    calls = {"n": 0}

    def _reply(_state):
        calls["n"] += 1
        return (1, "", err) if calls["n"] == 1 else (0, "", "")
    git["replies"]["push"] = _reply
    assert _pr._push("/repo", "aiforge/ONE-1") == (True, "")
    assert "--force-with-lease" in git["calls"][1]


def test_a_shared_branch_is_never_force_pushed(git, reachable):
    git["replies"]["push"] = (1, "", "non-fast-forward")
    ok, err = _pr._push("/repo", "main")
    assert ok is False
    assert "non-fast-forward" in err
    assert not any("--force-with-lease" in c for c in git["calls"])


def test_a_failed_lease_retry_reports_the_second_error(git, reachable):
    calls = {"n": 0}

    def _reply(_state):
        calls["n"] += 1
        return (1, "", "non-fast-forward") if calls["n"] == 1 else (1, "", "denied")
    git["replies"]["push"] = _reply
    assert _pr._push("/repo", "aiforge/ONE-1") == (False, "denied")


# ─── opening the PR ────────────────────────────────────────────────────


@pytest.fixture
def gh(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda exe: "/usr/bin/gh")


def test_a_pr_is_opened(git, gh):
    git["replies"]["pr create"] = (0, "https://github.com/o/r/pull/7\n", "")
    assert _pr._open_pr("/repo", "ONE-1", "Fix it", "body") == (
        "https://github.com/o/r/pull/7", "")


def test_without_the_gh_cli_the_push_still_counts(git, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda exe: None)
    assert _pr._open_pr("/repo", "ONE-1", "t", "b") == ("", "gh_not_installed")


def test_an_existing_pr_url_in_the_error_is_reused(git, gh):
    """The work IS shipped — dead-ending the ticket as blocked would be wrong."""
    git["replies"]["pr create"] = (
        1, "", "a pull request for branch already exists: "
               "https://github.com/o/r/pull/3")
    assert _pr._open_pr("/repo", "ONE-1", "t", "b") == (
        "https://github.com/o/r/pull/3", "")


def test_an_existing_pr_without_a_url_is_looked_up(git, gh):
    git["replies"]["pr create"] = (1, "", "already exists for this branch")
    git["replies"]["pr view"] = (0, "https://github.com/o/r/pull/4\n", "")
    assert _pr._open_pr("/repo", "ONE-1", "t", "b")[0] == \
        "https://github.com/o/r/pull/4"


def test_a_lookup_that_returns_nothing_reports_the_error(git, gh):
    git["replies"]["pr create"] = (1, "", "already exists")
    git["replies"]["pr view"] = (0, "  \n", "")
    url, err = _pr._open_pr("/repo", "ONE-1", "t", "b")
    assert url == ""
    assert "already exists" in err


def test_an_ordinary_gh_failure_is_reported(git, gh):
    git["replies"]["pr create"] = (1, "", "no upstream configured")
    url, err = _pr._open_pr("/repo", "ONE-1", "t", "b")
    assert url == ""
    assert "no upstream" in err


# ─── the whole flow ────────────────────────────────────────────────────


@pytest.fixture
def flow(monkeypatch):
    state = {"prod": ["app/store.py"], "test": ["tests/test_store.py"],
             "pushed": (True, ""), "pr": ("https://github.com/o/r/pull/1", ""),
             "events": [], "ingest": []}
    monkeypatch.setattr(_pr, "_resolve_repo_root", lambda: "/repo")
    monkeypatch.setattr(_pr, "_has_doer_changes", lambda root: (True, ""))
    monkeypatch.setattr(_pr, "_checkout_branch", lambda root, branch: "")
    monkeypatch.setattr(_pr, "_commit_changes", lambda root, ident, title: "")
    monkeypatch.setattr(_pr, "_classify_head_diff",
                        lambda root: (state["prod"], state["test"]))
    monkeypatch.setattr(_pr, "_push", lambda root, branch: state["pushed"])
    monkeypatch.setattr(_pr, "_open_pr",
                        lambda root, ident, title, body: state["pr"])
    monkeypatch.setattr(_pr, "_fire_delta_ingest",
                        lambda ticket, root: state["ingest"].append(ticket.identifier))
    monkeypatch.delenv("AIFORGE_ALLOW_TEST_ONLY_PR", raising=False)
    return state


def test_the_happy_path_returns_the_pr_url(flow, monkeypatch):
    import aiforge_core.runtime.observability as obs
    monkeypatch.setattr(obs, "emit_pr_opened",
                        lambda **kw: flow["events"].append(kw))
    out = _pr.commit_push_open_pr(_ticket())
    assert out == {"branch_pushed": True, "pr_url": "https://github.com/o/r/pull/1"}
    assert flow["events"][0]["branch"] == "aiforge/ONE-1"
    assert flow["ingest"] == ["ONE-1"]


def test_a_tree_that_is_not_a_repo(flow, monkeypatch):
    monkeypatch.setattr(_pr, "_resolve_repo_root", lambda: None)
    assert _pr.commit_push_open_pr(_ticket()) == {"pr_skip_reason": "not_a_git_repo"}


def test_no_changes_short_circuits(flow, monkeypatch):
    monkeypatch.setattr(_pr, "_has_doer_changes", lambda root: (False, "no_changes"))
    assert _pr.commit_push_open_pr(_ticket()) == {"pr_skip_reason": "no_changes"}


def test_a_failed_checkout_short_circuits(flow, monkeypatch):
    monkeypatch.setattr(_pr, "_checkout_branch",
                        lambda root, branch: "checkout_failed")
    assert _pr.commit_push_open_pr(_ticket())["pr_skip_reason"] == "checkout_failed"


def test_a_failed_commit_short_circuits(flow, monkeypatch):
    monkeypatch.setattr(_pr, "_commit_changes",
                        lambda root, i, t: "commit_failed")
    assert _pr.commit_push_open_pr(_ticket())["pr_skip_reason"] == "commit_failed"


def test_a_test_only_diff_is_rejected(flow):
    """Tests prove a fix; they are not the fix. ONE-3 published a no-op PR."""
    flow["prod"] = []
    out = _pr.commit_push_open_pr(_ticket())
    assert out["pr_skip_reason"] == "test_only_diff"
    assert out["test_only_files"] == ["tests/test_store.py"]


def test_a_test_only_diff_can_be_allowed(flow, monkeypatch):
    monkeypatch.setenv("AIFORGE_ALLOW_TEST_ONLY_PR", "1")
    flow["prod"] = []
    assert _pr.commit_push_open_pr(_ticket())["branch_pushed"] is True


def test_a_failed_push_reports_the_reason(flow):
    flow["pushed"] = (False, "no_origin_configured")
    assert _pr.commit_push_open_pr(_ticket()) == {
        "branch_pushed": False, "pr_skip_reason": "push_failed",
        "push_err": "no_origin_configured"}


def test_a_push_without_a_pr_still_records_the_push(flow):
    flow["pr"] = ("", "gh_not_installed")
    out = _pr.commit_push_open_pr(_ticket())
    assert out == {"branch_pushed": True, "pr_skip_reason": "gh_not_installed",
                   "gh_err": "gh_not_installed"}


def test_the_tickets_own_branch_is_used_when_set(flow, monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(_pr, "_checkout_branch",
                        lambda root, branch: seen.setdefault("branch", branch) and "")
    _pr.commit_push_open_pr(_ticket(branch="feature/manual"))
    assert seen["branch"] == "feature/manual"


def test_a_failed_event_emit_never_blocks_the_pr(flow, monkeypatch):
    import aiforge_core.runtime.observability as obs
    monkeypatch.setattr(obs, "emit_pr_opened",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("db down")))
    assert _pr.commit_push_open_pr(_ticket())["pr_url"]


# ─── the post-PR ingest ────────────────────────────────────────────────


@pytest.fixture
def ingest(monkeypatch):
    spawned: list = []
    monkeypatch.setattr(shutil, "which", lambda exe: "/usr/bin/aiforge-memory")
    monkeypatch.setattr(subprocess, "Popen",
                        lambda argv, **kw: spawned.append(argv))
    monkeypatch.delenv("AIFORGE_POST_PR_INGEST", raising=False)
    return spawned


def test_the_pushed_code_is_delta_ingested(ingest):
    """Without it, recall on the next ticket returns the pre-change code."""
    _pr._fire_delta_ingest(_ticket(project="AIForgeCrew"), "/repo")
    assert ingest[0][:3] == ["/usr/bin/aiforge-memory", "ingest", "AIForgeCrew"]
    assert "--delta" in ingest[0]


def test_the_ingest_can_be_turned_off(ingest, monkeypatch):
    monkeypatch.setenv("AIFORGE_POST_PR_INGEST", "0")
    _pr._fire_delta_ingest(_ticket(project="p"), "/repo")
    assert ingest == []


def test_a_ticket_with_no_project_is_not_ingested(ingest):
    _pr._fire_delta_ingest(_ticket(project=""), "/repo")
    assert ingest == []


def test_a_missing_cli_is_not_ingested(ingest, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda exe: None)
    _pr._fire_delta_ingest(_ticket(project="p"), "/repo")
    assert ingest == []


def test_a_failed_spawn_is_swallowed(ingest, monkeypatch):
    monkeypatch.setattr(subprocess, "Popen",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no exec")))
    _pr._fire_delta_ingest(_ticket(project="p"), "/repo")


# ─── merging ───────────────────────────────────────────────────────────


@pytest.fixture
def merge(monkeypatch):
    state = {"result": types.SimpleNamespace(returncode=0, stdout="merged",
                                             stderr=""),
             "args": None}
    monkeypatch.setattr(shutil, "which", lambda exe: "/usr/bin/gh")

    def _run(args, **kw):
        state["args"] = args
        return state["result"]
    monkeypatch.setattr(subprocess, "run", _run)
    return state


def test_a_pr_is_squash_merged_by_default(merge):
    assert _pr.merge_pr("https://github.com/o/r/pull/1") == {"merged": True,
                                                             "reason": ""}
    assert "--squash" in merge["args"]
    assert "--delete-branch" in merge["args"]


def test_a_merge_commit_can_be_requested(merge):
    _pr.merge_pr("https://github.com/o/r/pull/1", squash=False)
    assert "--merge" in merge["args"]


def test_an_already_merged_pr_is_not_a_failure(merge):
    """The deploy recipe may have merged it for a deploy_target ticket."""
    merge["result"] = types.SimpleNamespace(returncode=1, stdout="",
                                            stderr="Pull request already merged")
    assert _pr.merge_pr("u") == {"merged": True, "reason": "already_merged"}


def test_a_real_merge_failure_is_reported(merge):
    merge["result"] = types.SimpleNamespace(returncode=1, stdout="",
                                            stderr="checks are failing")
    out = _pr.merge_pr("u")
    assert out["merged"] is False
    assert "checks are failing" in out["reason"]


def test_merging_needs_a_url_and_the_cli(monkeypatch):
    assert _pr.merge_pr("") == {"merged": False, "reason": "no_pr_url"}
    monkeypatch.setattr(shutil, "which", lambda exe: None)
    assert _pr.merge_pr("u") == {"merged": False, "reason": "gh_not_installed"}
