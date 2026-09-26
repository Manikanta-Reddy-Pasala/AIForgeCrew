"""Team runs in a user's repo: no silent git init, no path false positives,
read-only paths enforced, the SPEC's plan followed, nothing left uncommitted.

Live run behind most of this: "In <repo> fix money.py so every test in tests/
passes. Run pytest there to check. Do not edit the tests." planned
``pyproject`` + ``test-money``, merged a rewritten tests/test_money.py onto the
user's branch, left the repair engine's edits uncommitted, and warned about a
dirty tree made dirty by its own SPEC.md.
"""
from __future__ import annotations

import os
import subprocess
from types import SimpleNamespace

import pytest

from aiforge_core.runtime import team_target as tt
from aiforge_core.runtime import team_workspace as tw
from aiforge_core.runtime.parallel_subtasks import _protected as prot
from aiforge_core.runtime.parallel_subtasks._spec_align import align_to_spec


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True)


def _repo(path):
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    (path / "money.py").write_text("def fmt(x):\n    return str(x)\n")
    (path / "tests").mkdir()
    (path / "tests" / "test_money.py").write_text("def test_x():\n    pass\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "init")
    return os.path.realpath(str(path))


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    for k in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(k, "t")
    for k in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(k, "t@t")
    yield
    prot._REG.clear()
    tw._RUNS.clear()


@pytest.fixture
def session_ws(tmp_path):
    ws = tmp_path / "chat-workspaces" / "session-1"
    ws.mkdir(parents=True)
    return str(ws)


# ─── H1 / M1: what counts as a target ──────────────────────────────────────


def test_an_output_file_in_tmp_is_not_a_build_target(session_ws):
    t = tt.resolve_team_target(["build a parser and write results to "
                                "/tmp/out.json"], session_ws)
    assert not t.retargeted and t.cwd == session_ws


@pytest.mark.parametrize("named", ["~/Documents", "~/", "~", "/", "/tmp"])
def test_home_root_and_system_folders_are_never_targets(tmp_path, session_ws,
                                                        monkeypatch, named):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home" / "Documents").mkdir(parents=True)
    t = tt.resolve_team_target([f"fix the scripts in {named}"], session_ws)
    assert not t.retargeted and t.cwd == session_ws


def test_dev_null_is_not_a_missing_folder(session_ws):
    t = tt.resolve_team_target(["run it with output to /dev/null and fix the "
                                "crash"], session_ws)
    assert not t.missing and not t.retargeted


def test_paths_in_a_pasted_log_or_code_block_are_not_targets(tmp_path,
                                                             session_ws):
    repo = _repo(tmp_path / "proj")
    text = ("the deploy fails, can you fix it?\n```\n$ cd " + repo +
            "\nError: boom\n```\n    at " + repo + "/x.py\n")
    t = tt.resolve_team_target([text], session_ws)
    assert not t.retargeted


def test_a_system_path_needs_a_directive(session_ws):
    assert tt._raw_paths("check /var/log/syslog, why is it slow") == []
    assert tt._raw_paths("work in /var/www/app please") == ["/var/www/app"]
    assert tt._raw_paths("fix /usr/local/bin/tool.py") == ["/usr/local/bin/tool.py"]
    assert tt._raw_paths("see /opt/app for details") == []


def test_git_root_lookup_is_cached_and_runs_no_subprocess(tmp_path,
                                                          monkeypatch):
    repo = _repo(tmp_path / "r")
    tt._git_root.cache_clear()

    def _no(*a, **k):
        raise AssertionError("no subprocess per path")
    monkeypatch.setattr(subprocess, "run", _no)
    assert tt._git_root(os.path.join(repo, "tests")) == repo
    assert tt._ROOT_CACHE[os.path.join(repo, "tests")] == repo
    assert tt._git_root(os.path.join(repo, "tests")) == repo


def _dispatch(prompt, cwd, session_id=None):
    from aiforge_core.api.routes._chat import _stages
    rd = SimpleNamespace(doc_task=False, route_pipeline=True, notice="")
    rctx = {"done": False}
    evs = list(_stages._dispatch_agent_route(
        rd, None, prompt, cwd, session_id, [{"role": "user", "content": prompt}],
        lambda t: t, {}, 0.0, False, "", rctx))
    return evs, rctx


def test_a_folder_that_is_not_a_repo_is_never_git_inited_silently(tmp_path,
                                                                 session_ws):
    d = tmp_path / "notes"
    d.mkdir()
    (d / "a.py").write_text("x = 1\n")
    evs, rctx = _dispatch(f"In {d} fix a.py", session_ws)
    assert rctx["done"] and evs[-1]["awaiting_input"]
    assert "not a git repository" in evs[-1]["text"]
    assert not (d / ".git").exists()


def test_a_non_repo_folder_is_initialised_only_after_allow(tmp_path,
                                                           session_ws,
                                                           monkeypatch):
    from aiforge_core.api.routes._chat import _team_route
    from aiforge_core.runtime import chat_write_grants
    d = tmp_path / "plain"
    d.mkdir()
    (d / "a.py").write_text("x = 1\n")
    asked = []

    def _consent(sid, folder, reason):
        asked.append(reason)
        yield {"type": "approval", "reason": reason}
        return False
    monkeypatch.setattr(_team_route, "_ask_consent", _consent)
    monkeypatch.setattr(chat_write_grants, "granted", lambda sid: [])
    evs, rctx = _dispatch(f"In {d} fix a.py", session_ws, session_id=7)
    assert asked and "git init" in asked[0]
    assert rctx["done"] and not (d / ".git").exists()


def test_approval_is_asked_before_working_in_a_user_repo(tmp_path, session_ws,
                                                         monkeypatch):
    from aiforge_core.api.routes._chat import _team_route
    from aiforge_core.runtime import chat_approve, chat_write_grants
    repo = _repo(tmp_path / "proj")
    monkeypatch.setattr(chat_approve, "approvals_required", lambda sid: True)
    monkeypatch.setattr(chat_write_grants, "granted", lambda sid: [])
    seen = []

    def _consent(sid, folder, reason):
        seen.append(folder)
        yield {"type": "approval", "reason": reason}
        return False
    monkeypatch.setattr(_team_route, "_ask_consent", _consent)
    evs, rctx = _dispatch(f"In {repo} fix money.py", session_ws, session_id=8)
    assert seen == [repo] and rctx["done"]
    assert "aiforge/" not in _git(repo, "branch").stdout


# ─── B1: read-only paths ───────────────────────────────────────────────────


def test_do_not_edit_the_tests_protects_every_test_file(tmp_path):
    root = str(tmp_path)
    prot.register(root, prot.from_texts(
        ["fix money.py so every test passes. Do not edit the tests."]))
    assert prot.is_protected(root, "tests/test_money.py")
    assert prot.is_protected(os.path.join(root, ".aiforge-worktrees", "x"),
                             "tests/test_money.py")
    assert not prot.is_protected(root, "money.py")


def test_spec_scope_lines_protect_only_their_objects():
    spec = (
        "- The only file that may be created or modified is `money.py`.\n"
        "- All files under `tests/` are read-only.\n"
        "- Do not modify `pyproject.toml`, `conftest.py`, or CI files.\n"
        "- Do not change the public behavior of `money.py` except as needed.\n"
        "- If a test cannot be fixed by modifying `money.py` only, stop; do "
        "not edit the test.\n")
    protected, only = prot.from_spec(spec)
    assert "tests/" in protected and "pyproject.toml" in protected
    assert "conftest.py" in protected and "money.py" not in protected
    assert only == ["money.py"]


def test_protected_subtasks_are_dropped_and_writes_refused(tmp_path):
    from aiforge_core.runtime.parallel_subtasks._reconcile._rewrite import _apply_patches
    from aiforge_core.runtime.parallel_subtasks._runners_write import _write_subtask_files
    root = str(tmp_path)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_money.py").write_text("assert 1\n")
    prot.register(root, [prot.TESTS])
    kept, dropped = prot.filter_subtasks(
        [{"slug": "money", "path": "money.py"},
         {"slug": "test-money", "path": "tests/test_money.py"}], root)
    assert [s["slug"] for s in kept] == ["money"]
    assert dropped == ["tests/test_money.py"]
    written, rejected, _ = _write_subtask_files(
        {"tests/test_money.py": "assert 0\n", "money.py": "x = 1\n"}, root, [])
    assert written == ["money.py"] and rejected == ["tests/test_money.py"]
    patch = ("### FILE: tests/test_money.py\n<<<<<<< SEARCH\nassert 1\n=======\n"
             "assert 0\n>>>>>>> REPLACE\n")
    w, failures = _apply_patches(root, patch)
    assert w == [] and "read-only" in failures[0][1]
    assert (tmp_path / "tests" / "test_money.py").read_text() == "assert 1\n"


def test_revert_puts_back_a_protected_file_a_writer_changed(tmp_path):
    repo = _repo(tmp_path / "r")
    prot.register(repo, [prot.TESTS])
    with open(os.path.join(repo, "tests", "test_money.py"), "a") as fh:
        fh.write("# weakened\n")
    with open(os.path.join(repo, "tests", "test_new.py"), "w") as fh:
        fh.write("x = 1\n")
    back = prot.revert(repo, "HEAD")
    assert sorted(back) == ["tests/test_money.py", "tests/test_new.py"]
    assert _git(repo, "status", "--porcelain").stdout == ""


def test_the_plan_follows_the_reviewed_spec():
    spec = ("## Subtasks (2)\n\n1. **money** — make minimal changes to "
            "`money.py` so all tests pass.\n2. **verify** — run `pytest`.\n")
    subs = [{"slug": "pyproject", "path": "pyproject.toml", "goal": "x"},
            {"slug": "test-money", "path": "tests/test_money.py", "goal": "t"},
            {"slug": "money", "path": "money.py", "goal": "m"}]
    out, dropped, added = align_to_spec(subs, spec)
    assert [s["path"] for s in out] == ["money.py"]
    assert dropped == ["pyproject", "test-money"] and added == []
    assert "minimal changes" in out[0]["goal"]


def test_an_unreviewed_spec_changes_nothing():
    from aiforge_core.runtime.parallel_subtasks._reconcile import _render_spec_md
    subs = [{"slug": "a", "path": "a.py", "goal": "a.py: x"},
            {"slug": "b", "path": "b.py", "goal": "b.py: y"}]
    out, dropped, added = align_to_spec(subs, _render_spec_md("build", subs))
    assert out == subs and not dropped and not added


# ─── H2 / B2 / B3 / H3: the run's own branch and worktree ─────────────────


def test_a_run_never_touches_the_users_branch_tree_or_gitignore(tmp_path):
    repo = _repo(tmp_path / "r")
    head = _git(repo, "rev-parse", "HEAD").stdout
    ws = tw.open_run(repo, "fix money.py")
    assert ws.branch.startswith("aiforge/fix-money-py")
    # SPEC lives beside the worktree, not in the repo
    p = tw.write_spec(ws.cwd, "# Project Spec\n")
    assert not p.startswith(repo) and not os.path.exists(
        os.path.join(ws.cwd, "SPEC.md"))
    # a repair engine leaves an uncommitted edit in the run worktree
    with open(os.path.join(ws.cwd, "money.py"), "a") as fh:
        fh.write("# repaired\n")
    open(os.path.join(ws.cwd, ".aiforge-baseline"), "w").write("x")
    assert tw.seal(ws.cwd) == ["money.py"]
    list(tw.close(ws))
    assert _git(repo, "rev-parse", "HEAD").stdout == head
    assert _git(repo, "status", "--porcelain").stdout == ""
    assert not (tmp_path / "r" / ".gitignore").exists()
    assert "# repaired" in _git(repo, "show", f"{ws.branch}:money.py").stdout
    assert ".aiforge-baseline" not in _git(
        repo, "show", "--name-only", ws.branch).stdout


def test_apply_on_my_branch_fast_forwards(tmp_path):
    repo = _repo(tmp_path / "r")
    assert tw.wants_apply(["fix it and commit it on my branch"])
    ws = tw.open_run(repo, "fix", apply=True)
    with open(os.path.join(ws.cwd, "money.py"), "a") as fh:
        fh.write("# done\n")
    notes = list(tw.close(ws))
    assert "fast-forward" in notes[-1]["text"]
    assert "# done" in (tmp_path / "r" / "money.py").read_text()


def test_dirty_check_ignores_untracked_and_own_artifacts(tmp_path):
    repo = _repo(tmp_path / "r")
    for f in ("SPEC.md", ".aiforge-baseline", "cache.tmp"):
        (tmp_path / "r" / f).write_text("x")
    assert tw.dirty_files(repo) == []
    (tmp_path / "r" / "money.py").write_text("changed\n")
    assert tw.dirty_files(repo) == ["money.py"]


def test_dirty_warning_ignores_the_pipelines_own_spec(tmp_path):
    from aiforge_core.runtime.parallel_subtasks import _dirty_warning
    repo = _repo(tmp_path / "r")
    (tmp_path / "r" / "SPEC.md").write_text("# Project Spec\n")
    assert _dirty_warning(repo) is None


def test_single_task_spec_never_overwrites_a_users_spec(tmp_path):
    repo = _repo(tmp_path / "r")
    (tmp_path / "r" / "SPEC.md").write_text("mine\n")
    _git(repo, "add", "SPEC.md")
    _git(repo, "commit", "-qm", "spec")
    p = tw.write_spec(repo, "# Project Spec\n")
    assert (tmp_path / "r" / "SPEC.md").read_text() == "mine\n"
    assert ".git" in p


def test_a_user_repo_keeps_its_gitignore(tmp_path):
    from aiforge_core.runtime.parallel_subtasks import _ensure_git_workspace
    repo = _repo(tmp_path / "r")
    _ensure_git_workspace(repo)
    assert not (tmp_path / "r" / ".gitignore").exists()
    assert ".aiforge-worktrees/" in (tmp_path / "r" / ".git" / "info" /
                                     "exclude").read_text()


# ─── B4: planning reviews run with reasoning off ───────────────────────────


def test_plan_and_spec_reviews_ask_for_no_reasoning(monkeypatch):
    import aiforge_core.llm.client as client
    from aiforge_core.runtime import review_gates as rg
    sent = {}

    def _complete(role, msgs, **kw):
        sent.update(kw)
        sent["text"] = msgs[-1]["content"]
        return "CLEAN"
    monkeypatch.setattr(client, "complete", _complete)
    monkeypatch.setattr(rg, "pick_reviewer_model", lambda: "a-thinker")
    monkeypatch.setattr(rg, "_fast_extras",
                        lambda: {"reasoning_effort": "none"})
    rg.review_spec("req", "# spec\n" + "x" * 100)
    assert sent["extras"]["reasoning_effort"] == "none"
    assert sent["max_tokens"] < 4096 and sent["text"].endswith("/no_think")
    monkeypatch.setenv("AIFORGE_REVIEW_PLANNING_THINK", "1")
    rg.review_spec("req", "# spec\n" + "x" * 100)
    assert "reasoning_effort" not in sent["extras"]


def test_the_import_pruner_never_edits_a_read_only_test(tmp_path):
    """Live run: the subtask's money.py lost ``fmt``; the dead-import pruner
    then deleted ``from money import fmt`` from the user's read-only test."""
    from aiforge_core.runtime.parallel_subtasks._reconcile._drift import _prune_dead_python_imports
    (tmp_path / "money.py").write_text("class Money:\n    pass\n")
    (tmp_path / "tests").mkdir()
    src = "from money import fmt\n\n\ndef test_x():\n    assert fmt(1)\n"
    (tmp_path / "tests" / "test_money.py").write_text(src)
    prot.register(str(tmp_path), [prot.TESTS])
    assert _prune_dead_python_imports(str(tmp_path)) == []
    assert (tmp_path / "tests" / "test_money.py").read_text() == src
