"""The pipeline Doer's repo tools: repo map, git, and rename.

The Doer builds its ADK tools from this module, not from the chat agent's —
which is why the codegraph wrappers are duplicated here at all: registered
anywhere else, the Doer simply never receives them.

git_commit is the interesting one. The Doer runs in an ISOLATED worktree
branched from a clean base, so everything changed there is its work and
staging is a plain ``git add -A`` — which, unlike the touched-file list it
replaced, also captures deletions and renames. Artifact pathspecs keep the
agent's own junk out. And because the Doer is told to commit after every
milestone, an empty tree has to be a success ("nothing to commit"), not the
error git would normally raise.
"""
from __future__ import annotations

import types as pytypes

import pytest

from aiforge_core.runtime.doer_tools import _repo as R


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_REPO_ROOT", str(tmp_path))
    return tmp_path


# ─── the repo map ──────────────────────────────────────────────────────


@pytest.fixture
def digest(monkeypatch):
    from aiforge_core.memory import code_context
    state: dict = {"digest": "app.py:\n  def main()\n", "seen": {}}
    monkeypatch.setattr(
        code_context, "aider_digest",
        lambda root, chat_files=None, token_budget=None, user_text="":
        state["seen"].update(root=root, budget=token_budget, text=user_text)
        or state["digest"])
    return state


def test_the_map_is_centred_on_what_was_asked_about(digest, sandbox):
    res = R.repo_map(focus="push sync", token_budget=2048)
    assert res["ok"] is True
    assert res["engine"] == "aider-treesitter-pagerank"
    assert digest["seen"]["text"] == "push sync"
    assert digest["seen"]["budget"] == 2048


def test_a_repo_too_small_to_map_says_so(digest, sandbox):
    digest["digest"] = ""
    res = R.repo_map()
    assert res["ok"] is False
    assert "empty map" in res["error"]


def test_a_missing_repomap_backend_is_a_soft_error(monkeypatch, sandbox):
    import builtins
    real = builtins.__import__

    def _imp(name, *a, **k):
        if name == "aiforge_core.memory.code_context":
            raise ImportError("no tree-sitter")
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", _imp)
    assert "repo_map unavailable" in R.repo_map()["error"]


def test_the_files_a_digest_lists_are_pulled_out():
    digest = ("app/main.py:\n"
              "│  def run()\n"
              "⋮\n"
              "lib/util.ts:\n"
              "│  export const x\n"
              "not a header\n")
    assert R._digest_file_paths(digest) == ["app/main.py", "lib/util.ts"]


def test_the_file_list_is_capped():
    digest = "\n".join(f"f{i}.py:" for i in range(40))
    assert len(R._digest_file_paths(digest)) == 20


# ─── the codegraph wrappers the Doer needs ─────────────────────────────


@pytest.mark.parametrize("tool,fn,kwarg", [
    (R.codegraph_impact, "codegraph_impact", "symbol"),
    (R.codegraph_callers, "codegraph_callers", "symbol"),
    (R.codegraph_callees, "codegraph_callees", "symbol"),
    (R.codegraph_explore, "codegraph_explore", "query"),
    (R.codegraph_query, "codegraph_query", "query"),
])
def test_each_wrapper_forwards_to_the_real_tool(tool, fn, kwarg, monkeypatch,
                                                sandbox):
    from aiforge_core.runtime.tools import codegraph
    monkeypatch.setattr(codegraph, fn,
                        lambda args, cwd: {"ok": True, "args": args,
                                           "cwd": cwd})
    res = tool("publishToRemoteServer")
    assert res["args"] == {kwarg: "publishToRemoteServer"}
    assert res["cwd"] == str(sandbox)


# ─── impacted tests ────────────────────────────────────────────────────


def test_the_changed_files_are_mapped_to_their_tests(monkeypatch):
    from aiforge_core.runtime import diff_impact
    monkeypatch.setenv("AIFORGE_AFM_REPO", "/repo")
    monkeypatch.setattr(diff_impact, "impacted_tests",
                        lambda repo, files: ["test_a.py", "test_b.py"])
    res = R.impacted_tests("src/a.py, src/b.py")
    assert res["tests"] == ["test_a.py", "test_b.py"]
    assert res["count"] == 2
    assert res["pattern"] == "test_a.py,test_b.py"


def test_nothing_changed_means_nothing_to_map(monkeypatch):
    monkeypatch.setenv("AIFORGE_AFM_REPO", "/repo")
    assert R.impacted_tests("  ")["error"] == "no changed files given"


def test_without_a_repo_the_caller_runs_the_full_suite(monkeypatch):
    monkeypatch.delenv("AIFORGE_AFM_REPO", raising=False)
    res = R.impacted_tests("a.py")
    assert res["ok"] is False
    assert res["tests"] == []


def test_a_failing_impact_analysis_is_soft(monkeypatch):
    from aiforge_core.runtime import diff_impact
    monkeypatch.setenv("AIFORGE_AFM_REPO", "/repo")
    monkeypatch.setattr(diff_impact, "impacted_tests",
                        lambda repo, files: (_ for _ in ()).throw(OSError("io")))
    assert R.impacted_tests("a.py") == {"ok": False, "error": "io", "tests": []}


# ─── committing ────────────────────────────────────────────────────────


@pytest.fixture
def git(monkeypatch, sandbox):
    """A scripted git: one canned result per subcommand."""
    state: dict = {"calls": [], "add_rc": 0, "diff_rc": 1, "commit_rc": 0,
                   "staged": "a.py\nb.py\n", "stdout": b"[main abc] msg",
                   "stderr": b""}

    def _run(argv, **kw):
        state["calls"].append(list(argv))
        sub = argv[1]
        if sub == "add":
            return pytypes.SimpleNamespace(returncode=state["add_rc"],
                                           stdout=b"", stderr=state["stderr"])
        if sub == "diff" and "--name-only" in argv:
            return pytypes.SimpleNamespace(returncode=0,
                                           stdout=state["staged"].encode(),
                                           stderr=b"")
        if sub == "diff":
            return pytypes.SimpleNamespace(returncode=state["diff_rc"],
                                           stdout=b"", stderr=state["stderr"])
        return pytypes.SimpleNamespace(returncode=state["commit_rc"],
                                       stdout=state["stdout"],
                                       stderr=state["stderr"])
    monkeypatch.setattr(R.subprocess, "run", _run)
    return state


def test_everything_in_the_worktree_is_staged_and_committed(git):
    res = R.git_commit("models written")
    assert res["ok"] is True
    assert res["staged"] == ["a.py", "b.py"]
    add = git["calls"][0]
    assert add[:4] == ["git", "add", "-A", "--"], \
        "-A also captures deletions and renames"
    assert any(p.startswith(":(exclude)") or p.startswith("!") or ":!" in p
               for p in add[4:]) or len(add) > 5, "artifact excludes are passed"


def test_a_milestone_with_nothing_new_is_a_success(git):
    """The Doer commits after every milestone; an empty tree must not error."""
    git["diff_rc"] = 0
    assert R.git_commit("nothing changed") == {"ok": True,
                                               "skipped": "nothing to commit"}
    assert not any(c[1] == "commit" for c in git["calls"])


def test_an_empty_message_is_refused_before_touching_git(git):
    assert R.git_commit("  ")["error"] == "empty commit message"
    assert git["calls"] == []


def test_a_failed_stage_reports_gits_own_words(git):
    git["add_rc"] = 128
    git["stderr"] = b"fatal: not a git repository"
    res = R.git_commit("msg")
    assert res["error"] == "git_add_failed"
    assert "fatal" in res["stderr"]


def test_a_broken_diff_is_not_read_as_an_empty_tree(git):
    """0 means nothing staged and 1 means staged changes — anything else is a
    git error, not a reason to skip."""
    git["diff_rc"] = 129
    assert R.git_commit("msg")["error"] == "git_diff_failed"


def test_a_rejected_commit_surfaces_both_streams(git):
    git["commit_rc"] = 1
    git["stdout"] = b"nothing added"
    git["stderr"] = b"hook failed"
    res = R.git_commit("msg")
    assert res["error"] == "git_commit_failed"
    assert "hook failed" in res["stderr"]
    assert "nothing added" in res["stdout"]


# ─── read-only git inspect ─────────────────────────────────────────────


@pytest.fixture
def git_ro(monkeypatch, sandbox):
    state: dict = {"argv": None, "rc": 0, "out": "on branch main\n", "err": ""}

    def _run(argv, cwd=None, capture_output=None, text=None, timeout=None):
        state["argv"] = list(argv)
        state["cwd"] = cwd
        if isinstance(state["rc"], Exception):
            raise state["rc"]
        return pytypes.SimpleNamespace(returncode=state["rc"],
                                       stdout=state["out"], stderr=state["err"])
    monkeypatch.setattr("subprocess.run", _run)
    return state


def test_status_is_read_in_porcelain_with_the_branch(git_ro, sandbox):
    res = R.git_status()
    assert res == {"ok": True, "code": 0, "stdout": "on branch main\n",
                   "stderr": ""}
    assert git_ro["argv"] == ["git", "status", "--porcelain=v1", "-b"]
    assert git_ro["cwd"] == str(sandbox)


def test_a_diff_can_be_narrowed_to_a_path(git_ro):
    R.git_diff(path="src/a.py", staged=True)
    assert git_ro["argv"] == ["git", "--no-pager", "diff", "--staged", "--",
                              "src/a.py"]


def test_a_plain_diff_asks_for_no_pager(git_ro):
    R.git_diff()
    assert git_ro["argv"] == ["git", "--no-pager", "diff"]


@pytest.mark.parametrize("asked,used", [(5, "-5"), (0, "-20"), (9999, "-200")])
def test_the_log_window_is_bounded(git_ro, asked, used):
    R.git_log(limit=asked)
    assert used in git_ro["argv"]


def test_blame_can_be_limited_to_a_line_range(git_ro):
    R.git_blame("a.py", start=10, end=20)
    assert git_ro["argv"][-4:] == ["-L", "10,20", "--", "a.py"]


def test_blame_without_a_range_covers_the_file(git_ro):
    R.git_blame("a.py")
    assert "-L" not in git_ro["argv"]


def test_a_nonzero_exit_is_reported_not_raised(git_ro):
    git_ro["rc"] = 128
    git_ro["err"] = "fatal: bad revision"
    res = R.git_status()
    assert res["ok"] is False
    assert res["code"] == 128


def test_a_box_without_git_says_exactly_that(git_ro):
    git_ro["rc"] = FileNotFoundError("git")
    assert R.git_status() == {"ok": False, "error": "git_not_installed"}


def test_a_hung_git_is_a_soft_error(git_ro):
    git_ro["rc"] = OSError("timed out")
    assert R.git_status()["error"] == "timed out"


def test_a_huge_diff_is_tail_capped(git_ro):
    git_ro["out"] = "d" * 20000
    assert len(R.git_diff()["stdout"]) == 8000


# ─── renaming ──────────────────────────────────────────────────────────


@pytest.fixture
def project(sandbox):
    (sandbox / "a.py").write_text("old = 1\nprint(old)\n")
    (sandbox / "sub").mkdir()
    (sandbox / "sub" / "b.go").write_text("old := 2\n")
    (sandbox / "README.md").write_text("old everywhere\n")
    (sandbox / "node_modules").mkdir()
    (sandbox / "node_modules" / "c.py").write_text("old\n")
    return sandbox


def test_a_rename_previews_before_it_touches_anything(project):
    res = R.rename_symbol("old", "new")
    assert res["dry_run"] is True
    assert res["total_occurrences"] == 3
    assert res["applied"] == 0
    assert "pass dry_run=false" in res["note"]
    assert (project / "a.py").read_text() == "old = 1\nprint(old)\n"


def test_applying_it_rewrites_and_records_the_touch(project, monkeypatch):
    touched: list = []
    monkeypatch.setattr(R, "record_touch", lambda fp: touched.append(fp))
    res = R.rename_symbol("old", "new", dry_run=False)
    assert res["applied"] == 3
    assert "review the diff" in res["note"]
    assert (project / "a.py").read_text() == "new = 1\nprint(new)\n"
    assert len(touched) == 2, "both source files are recorded as edited"


def test_vendor_directories_and_non_code_files_are_left_alone(project):
    res = R.rename_symbol("old", "new", dry_run=False)
    files = [h["file"] for h in res["files"]]
    assert "README.md" not in files
    assert (project / "node_modules" / "c.py").read_text() == "old\n"


def test_both_names_are_required(project):
    assert R.rename_symbol("", "new")["ok"] is False
    assert R.rename_symbol("old", "")["ok"] is False


def test_a_word_boundary_keeps_a_longer_identifier_intact(project):
    (project / "a.py").write_text("older = 1\nold = 2\n")
    R.rename_symbol("old", "new", dry_run=False)
    assert (project / "a.py").read_text() == "older = 1\nnew = 2\n"


def test_an_unreadable_file_counts_as_no_occurrences(project):
    import re
    assert R._rename_in_one_file(str(project / "ghost.py"),
                                 re.compile(r"\bold\b"), "new", False) == 0


def test_a_file_that_cannot_be_written_is_not_counted(project, monkeypatch):
    import builtins
    import re
    real = builtins.open

    def _open(path, mode="r", **kw):
        if "w" in mode:
            raise PermissionError("read-only")
        return real(path, mode, **kw)
    monkeypatch.setattr(builtins, "open", _open)
    assert R._rename_in_one_file(str(project / "a.py"), re.compile(r"\bold\b"),
                                 "new", False) == 0
