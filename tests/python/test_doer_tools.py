"""Unit tests for the v6 Doer's filesystem + intelligence tools.

Network-free. Each test uses a per-test ``AIFORGE_REPO_ROOT`` so writes
are sandboxed inside a tmp_path and never touch the operator's real
workspace.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from aiforge_core.runtime import doer_tools as dt


@pytest.fixture(autouse=True)
def _isolated_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AIFORGE_REPO_ROOT", str(tmp_path))
    monkeypatch.delenv("AIFORGE_DOER_SKIP_SYNTAX", raising=False)
    # The touched-path tracker is module-global; clear it so each test
    # starts clean and doesn't inherit paths from a prior test's writes.
    dt.reset_touched()
    return tmp_path


# ─── _validate_syntax — direct unit checks ────────────────────────────


def test_validate_python_compile_ok() -> None:
    ok, err = dt.validate_syntax("a.py", "def f():\n    return 1\n")
    assert ok and err == ""


def test_validate_python_syntax_error() -> None:
    ok, err = dt.validate_syntax("a.py", "def f(:\n    pass\n")
    assert not ok
    assert "(" in err or "syntax" in err.lower()


def test_validate_unbalanced_braces() -> None:
    ok, err = dt.validate_syntax("x.java", "class X { void f() { }")
    assert not ok
    assert "{}" in err


def test_validate_unbalanced_parens() -> None:
    ok, err = dt.validate_syntax("x.go", "func f(int x { }")
    assert not ok


def test_validate_empty_rejected() -> None:
    ok, err = dt.validate_syntax("a.py", "")
    assert not ok
    assert "empty" in err


def test_validate_java_python_kwargs_rejected() -> None:
    src = "class X {\n  void f() {\n    helper(name = bar, value = baz);\n  }\n}\n"
    ok, err = dt.validate_syntax("X.java", src)
    assert not ok
    assert "kwargs" in err


def test_validate_java_annotation_with_eq_allowed() -> None:
    # @Bean(name = "x") looks like kwargs but is valid Java annotation.
    src = (
        "package a;\n"
        "@Bean(name = \"thing\")\n"
        "public class X {\n"
        "  public void f() {}\n"
        "}\n"
    )
    ok, err = dt.validate_syntax("X.java", src)
    assert ok, err


def test_validate_unknown_extension_passes_when_balanced() -> None:
    ok, err = dt.validate_syntax("notes.txt", "hello world\n")
    assert ok, err


# ─── file_write integration ───────────────────────────────────────────


def test_file_write_rejects_corrupt_python(tmp_path: Path) -> None:
    res = dt.file_write("broken.py", "def f(:\n  return 1\n")
    assert res["ok"] is False
    assert "syntax_invalid" in res["error"]
    # File NOT written.
    assert not (tmp_path / "broken.py").exists()


def test_file_write_writes_clean_python(tmp_path: Path) -> None:
    res = dt.file_write("good.py", "x = 1\n")
    assert res["ok"] is True
    assert (tmp_path / "good.py").read_text() == "x = 1\n"


def test_file_write_skip_syntax_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIFORGE_DOER_SKIP_SYNTAX", "1")
    res = dt.file_write("legacy.py", "def f(:\n  pass\n")
    # Bypassed — file IS written despite invalid syntax.
    assert res["ok"] is True
    assert (tmp_path / "legacy.py").exists()


def test_file_write_path_traversal_blocked() -> None:
    res = dt.file_write("../escape.txt", "nope")
    assert res["ok"] is False
    assert "outside" in res["error"].lower() or "permission" in res["error"].lower()


# ─── memory_lookup ─────────────────────────────────────────────────────


def test_memory_lookup_handles_missing_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When unified_query raises, the tool returns ok=False with a clean
    error string instead of bubbling — the agent loop must not crash on
    a flaky memory backend."""
    # Patch the real module's ``query`` attribute (order-independent):
    # swapping sys.modules is ignored once the submodule is bound as a
    # package attribute by any prior import.
    from aiforge_core.memory import unified_query as uq

    def _boom(*a, **kw):
        raise RuntimeError("backend down")

    monkeypatch.setattr(uq, "query", _boom)
    res = dt.memory_lookup("anything")
    assert res["ok"] is False
    assert "backend down" in res["error"]


def test_memory_lookup_caps_k(monkeypatch: pytest.MonkeyPatch) -> None:
    """k > 12 should be clamped — guards against the model passing huge
    values that would dump the entire memory bank into the context."""
    from aiforge_core.memory import unified_query as uq

    def _stub_query(text, **kw):
        return {
            "hits": [
                {"source": "m", "score": 0.5, "text": f"row{i}"}
                for i in range(20)
            ],
            "used_sources": ["memory"],
        }

    monkeypatch.setattr(uq, "query", _stub_query)
    res = dt.memory_lookup("query", k=999)
    assert res["ok"] is True
    assert len(res["hits"]) <= 12


# ─── grep_repo ─────────────────────────────────────────────────────────


def test_grep_repo_finds_match(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def alpha():\n    return 1\n")
    (tmp_path / "b.py").write_text("def beta():\n    return 2\n")
    res = dt.grep_repo(r"def alpha")
    assert res["ok"] is True
    assert len(res["hits"]) == 1
    assert res["hits"][0]["file"] == "a.py"
    assert res["hits"][0]["line"] == 1
    assert "alpha" in res["hits"][0]["text"]


def test_grep_repo_path_subdir(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "x.py").write_text("class Widget:\n    pass\n")
    (tmp_path / "other.py").write_text("class Widget:\n    pass\n")
    res = dt.grep_repo(r"class Widget", path="src")
    assert res["ok"] is True
    files = {h["file"] for h in res["hits"]}
    assert all(f.startswith("src/") for f in files)


def test_grep_repo_no_match(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n")
    res = dt.grep_repo(r"NEVER_MATCHES_xyz123")
    assert res["ok"] is True
    assert res["hits"] == []


def test_grep_repo_empty_pattern_rejected() -> None:
    res = dt.grep_repo("")
    assert res["ok"] is False


def test_grep_repo_nonexistent_path() -> None:
    res = dt.grep_repo("foo", path="does/not/exist")
    assert res["ok"] is False


def test_grep_repo_skips_excluded_dirs(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("MAGIC_TOKEN\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "lib.js").write_text("MAGIC_TOKEN\n")
    (tmp_path / "src.py").write_text("MAGIC_TOKEN\n")
    res = dt.grep_repo("MAGIC_TOKEN")
    files = {h["file"] for h in res["hits"]}
    assert "src.py" in files
    assert not any(".git" in f or "node_modules" in f for f in files)


# ─── fetch_url ─────────────────────────────────────────────────────────


def test_fetch_url_rejects_non_http(monkeypatch: pytest.MonkeyPatch) -> None:
    # Opt in past the lockdown gate so we exercise the scheme check itself.
    monkeypatch.setenv("AIFORGE_ALLOW_WEB_FETCH", "1")
    assert dt.fetch_url("file:///etc/passwd")["ok"] is False
    assert dt.fetch_url("ftp://example.com")["ok"] is False
    assert dt.fetch_url("")["ok"] is False


def test_fetch_url_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lockdown default: arbitrary fetch is off, no request made."""
    monkeypatch.delenv("AIFORGE_ALLOW_WEB_FETCH", raising=False)
    res = dt.fetch_url("https://example.com")
    assert res["ok"] is False
    assert "web fetch disabled" in res["error"]


def test_fetch_url_handles_urlerror(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.error

    monkeypatch.setenv("AIFORGE_ALLOW_WEB_FETCH", "1")

    def _boom(*a, **kw):
        raise urllib.error.URLError("dns fail")

    monkeypatch.setattr("urllib.request.urlopen", _boom)
    res = dt.fetch_url("https://example.invalid")
    assert res["ok"] is False
    assert "url error" in res["error"]


def test_fetch_url_caps_body_size(monkeypatch: pytest.MonkeyPatch) -> None:
    """Body larger than 256 KB → truncated=True, body capped."""
    monkeypatch.setenv("AIFORGE_ALLOW_WEB_FETCH", "1")
    big = b"X" * (300 * 1024)

    class _Resp:
        status = 200
        headers = {"Content-Type": "text/plain"}

        def __init__(self, payload: bytes) -> None:
            self._payload = payload
            self._read = False

        def read(self, n: int = -1) -> bytes:
            if self._read:
                return b""
            self._read = True
            return self._payload[:n] if n > 0 else self._payload

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

    monkeypatch.setattr(
        "urllib.request.urlopen", lambda *a, **kw: _Resp(big),
    )
    res = dt.fetch_url("https://example.com/big")
    assert res["ok"] is True
    assert res["truncated"] is True
    assert len(res["body"].encode("utf-8")) <= 256 * 1024


def test_fetch_url_handles_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.error

    monkeypatch.setenv("AIFORGE_ALLOW_WEB_FETCH", "1")

    def _boom(*a, **kw):
        raise urllib.error.HTTPError(
            "https://x", 404, "not found", {}, None,
        )

    monkeypatch.setattr("urllib.request.urlopen", _boom)
    res = dt.fetch_url("https://example.com/missing")
    assert res["ok"] is False
    assert res["status"] == 404


# ─── adk_function_tools registry ───────────────────────────────────────


def test_adk_function_tools_includes_new_tools() -> None:
    """Registry must expose grep_repo + fetch_url + their aliases."""
    pytest.importorskip("google.adk")
    tools = dt.adk_function_tools()
    names = {t.func.__name__ for t in tools}
    assert "grep_repo" in names
    assert "fetch_url" in names
    # Aliases stay reachable for hallucinated names
    assert "grep" in names and "search" in names
    assert "http_get" in names and "web_fetch" in names


def test_module_all_lists_new_tools() -> None:
    assert "grep_repo" in dt.__all__
    assert "fetch_url" in dt.__all__


# ─── git_commit ────────────────────────────────────────────────────────


def _git_init(repo: Path) -> None:
    """Initialise an isolated git repo without touching the global config."""
    import subprocess as _sp

    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    _sp.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True, env=env)
    _sp.run(["git", "config", "user.email", "test@example.com"],
            cwd=repo, check=True)
    _sp.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    _sp.run(["git", "config", "commit.gpgsign", "false"],
            cwd=repo, check=True)


def test_git_commit_success_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A staged change should produce a real commit; tool returns ok=True."""
    if shutil.which("git") is None:
        pytest.skip("git binary not on PATH")
    _git_init(tmp_path)
    # Need an initial commit so HEAD exists for diff.
    (tmp_path / "seed.txt").write_text("seed\n")
    import subprocess as _sp
    _sp.run(["git", "add", "seed.txt"], cwd=tmp_path, check=True)
    _sp.run(["git", "commit", "-m", "init", "-q"], cwd=tmp_path, check=True)

    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.com")
    (tmp_path / "feature.py").write_text("x = 1\n")

    res = dt.git_commit("feat: add feature module")
    assert res["ok"] is True, res
    assert res.get("skipped") is None
    log = _sp.run(
        ["git", "log", "--oneline"], cwd=tmp_path,
        capture_output=True, text=True, check=True,
    )
    assert "feat: add feature module" in log.stdout


def test_git_commit_skips_when_nothing_staged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty working tree → skipped marker, no error, no new commit."""
    if shutil.which("git") is None:
        pytest.skip("git binary not on PATH")
    _git_init(tmp_path)
    (tmp_path / "seed.txt").write_text("seed\n")
    import subprocess as _sp
    _sp.run(["git", "add", "seed.txt"], cwd=tmp_path, check=True)
    _sp.run(["git", "commit", "-m", "init", "-q"], cwd=tmp_path, check=True)

    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.com")

    before = _sp.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path,
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    res = dt.git_commit("nothing changed")
    assert res["ok"] is True
    assert res.get("skipped") == "nothing to commit"

    after = _sp.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path,
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert before == after, "HEAD must not move when nothing was committed"


def test_git_commit_soft_errors_on_add_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulated `git add` failure must return ok=False, not raise."""
    import subprocess as real_sp

    real_run = real_sp.run

    def _fake_run(cmd, *a, **kw):
        if isinstance(cmd, list) and cmd[:2] == ["git", "add"]:
            class _Result:
                returncode = 1
                stdout = b""
                stderr = b"fatal: simulated add failure"
            return _Result()
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr("aiforge_core.runtime.doer_tools.subprocess.run", _fake_run)
    res = dt.git_commit("anything")
    assert res["ok"] is False
    assert res["error"] == "git_add_failed"
    assert "simulated add failure" in res["stderr"]


def test_git_commit_rejects_empty_message() -> None:
    """Guard rail: empty/whitespace messages produce ok=False without
    invoking git at all."""
    res = dt.git_commit("")
    assert res["ok"] is False
    res = dt.git_commit("   ")
    assert res["ok"] is False


def test_git_commit_in_function_tools_registry() -> None:
    """ADK registry must expose git_commit + its aliases."""
    pytest.importorskip("google.adk")
    tools = dt.adk_function_tools()
    names = {t.func.__name__ for t in tools}
    assert "git_commit" in names
    assert "commit" in names
    assert "git_add_commit" in names


def test_git_commit_in_module_all() -> None:
    assert "git_commit" in dt.__all__
    assert "commit" in dt.__all__
    assert "git_add_commit" in dt.__all__


# ── hallucinated tool-name aliases (local-model support) ──────────────


def test_edit_alias_delegates_to_file_patch(tmp_path, monkeypatch) -> None:
    """ONE-7 regression: local Qwen calls `edit`; it must mutate the file
    via file_patch instead of failing 'Tool edit not found'."""
    import os
    from aiforge_core.runtime import doer_tools as dt

    monkeypatch.setenv("AIFORGE_REPO_ROOT", str(tmp_path))
    f = tmp_path / "Foo.java"
    f.write_text("class Foo { int x = 1; }\n")
    out = dt.edit("Foo.java", "int x = 1;", "int x = 2;")
    assert isinstance(out, dict)
    assert "int x = 2;" in f.read_text()


def test_str_replace_alias_delegates(tmp_path, monkeypatch) -> None:
    from aiforge_core.runtime import doer_tools as dt
    monkeypatch.setenv("AIFORGE_REPO_ROOT", str(tmp_path))
    f = tmp_path / "a.txt"
    f.write_text("hello world")
    dt.str_replace("a.txt", "world", "there")
    assert f.read_text() == "hello there"


def test_edit_registered_in_adk_tools() -> None:
    from aiforge_core.runtime.doer_tools import adk_function_tools
    names = {
        getattr(t, "name", getattr(getattr(t, "func", None), "__name__", ""))
        for t in adk_function_tools()
    }
    assert "edit" in names
    assert "str_replace" in names


def test_meta_tool_noops_registered_and_safe() -> None:
    """Local models emit todo_write/glob/task from training; they must
    be registered no-ops (not pipeline-killers) — ONE-7 regression."""
    from aiforge_core.runtime.doer_tools import adk_function_tools
    names = {
        getattr(t, "name", getattr(getattr(t, "func", None), "__name__", ""))
        for t in adk_function_tools()
    }
    for n in ("todo_write", "todowrite", "glob", "task"):
        assert n in names, n


def test_todo_write_is_noop_ok() -> None:
    from aiforge_core.runtime import doer_tools as dt
    assert dt.todo_write("[x] do thing")["ok"] is True
    assert dt.task("spawn researcher")["ok"] is True


# ─── touched-path tracker + scoped git_commit ─────────────────────────


def test_record_touch_and_paths_roundtrip(tmp_path: Path) -> None:
    dt.reset_touched()
    assert dt.touched_paths() == []
    dt.record_touch("a/b.py")
    dt.record_touch("c.txt")
    dt.record_touch("a/b.py")           # dedup
    assert dt.touched_paths() == ["a/b.py", "c.txt"]


def test_reset_touched_clears(tmp_path: Path) -> None:
    dt.record_touch("x.py")
    assert dt.touched_paths()
    dt.reset_touched()
    assert dt.touched_paths() == []


def test_file_write_records_touch(tmp_path: Path) -> None:
    dt.reset_touched()
    dt.file_write("mod.py", "x = 1\n")
    assert "mod.py" in dt.touched_paths()


def test_file_patch_records_touch(tmp_path: Path) -> None:
    dt.reset_touched()
    (tmp_path / "a.txt").write_text("hello world")
    dt.file_patch("a.txt", "world", "there")
    assert "a.txt" in dt.touched_paths()


def test_git_commit_stages_all_worktree_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Isolated worktree ⇒ everything changed is the agent's work: both a
    file-tool write AND a shell-written file get staged + committed."""
    if shutil.which("git") is None:
        pytest.skip("git binary not on PATH")
    _git_init(tmp_path)
    (tmp_path / "seed.txt").write_text("seed\n")
    import subprocess as _sp
    _sp.run(["git", "add", "seed.txt"], cwd=tmp_path, check=True)
    _sp.run(["git", "commit", "-m", "init", "-q"], cwd=tmp_path, check=True)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.com")

    dt.reset_touched()
    dt.file_write("tracked.py", "x = 1\n")            # via file tool
    (tmp_path / "untracked.py").write_text("y = 2\n")  # via the shell

    res = dt.git_commit("feat: all changes")
    assert res["ok"] is True, res
    assert res["staged_via"] == "add_all"
    assert set(res["staged"]) == {"tracked.py", "untracked.py"}
    show = _sp.run(["git", "show", "--name-only", "--format=", "HEAD"],
                   cwd=tmp_path, capture_output=True, text=True, check=True)
    assert "tracked.py" in show.stdout
    assert "untracked.py" in show.stdout


def test_git_commit_stages_deletions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file written then removed in the worktree must be committed as a
    DELETION (the old touched-list path dropped deletes)."""
    if shutil.which("git") is None:
        pytest.skip("git binary not on PATH")
    _git_init(tmp_path)
    (tmp_path / "gone.txt").write_text("bye\n")
    import subprocess as _sp
    _sp.run(["git", "add", "gone.txt"], cwd=tmp_path, check=True)
    _sp.run(["git", "commit", "-m", "init", "-q"], cwd=tmp_path, check=True)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.com")

    dt.reset_touched()
    dt.file_write("added.py", "a = 1\n")
    (tmp_path / "gone.txt").unlink()                 # delete a tracked file

    res = dt.git_commit("feat: add + delete")
    assert res["ok"] is True, res
    show = _sp.run(["git", "show", "--name-status", "--format=", "HEAD"],
                   cwd=tmp_path, capture_output=True, text=True, check=True)
    assert "A\tadded.py" in show.stdout
    assert "D\tgone.txt" in show.stdout


def test_git_commit_excludes_artifacts_from_touched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An artifact path recorded as touched must be filtered out of the
    staged set."""
    if shutil.which("git") is None:
        pytest.skip("git binary not on PATH")
    _git_init(tmp_path)
    (tmp_path / "seed.txt").write_text("seed\n")
    import subprocess as _sp
    _sp.run(["git", "add", "seed.txt"], cwd=tmp_path, check=True)
    _sp.run(["git", "commit", "-m", "init", "-q"], cwd=tmp_path, check=True)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.com")

    dt.reset_touched()
    dt.file_write("good.py", "x = 1\n")
    # An agent artifact slips into the tracker; create it on disk too.
    (tmp_path / ".aiforge-worktrees").mkdir()
    (tmp_path / ".aiforge-worktrees" / "junk.txt").write_text("junk\n")
    dt.record_touch(".aiforge-worktrees/junk.txt")

    res = dt.git_commit("feat: good only")
    assert res["ok"] is True, res
    assert res["staged"] == ["good.py"]
