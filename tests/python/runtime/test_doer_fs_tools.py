"""The Doer's filesystem and shell tools.

These are the tools a local model calls unattended, so the guards matter more
than the happy paths: a "rewrite" that drops most of a file is refused, a draft
that does not parse never reaches disk, a dangerous shell command is blocked,
and a safety classifier that itself errors FAILS CLOSED rather than turning
into arbitrary execution. Everything runs against a temp repo root.
"""
from __future__ import annotations

import os
import subprocess

import pytest

from aiforge_core.runtime import sandbox
from aiforge_core.runtime.doer_tools import _fs


@pytest.fixture(autouse=True)
def repo(monkeypatch, tmp_path):
    """A temp repo root, a clean read cache and a clean touch tracker."""
    monkeypatch.setattr(sandbox, "_ROOT_OVERRIDE", sandbox._ROOT_OVERRIDE)
    monkeypatch.setenv("AIFORGE_REPO_ROOT", str(tmp_path))
    from aiforge_core.runtime import request_context
    monkeypatch.setattr(request_context, "get_repo_root", lambda: str(tmp_path))
    _fs._READ_CACHE.clear()
    _fs.reset_touched()
    for k in ("AIFORGE_ALLOW_TRUNCATION", "AIFORGE_DOER_SKIP_SYNTAX",
              "AIFORGE_TRUNCATE_MIN_LINES", "AIFORGE_TRUNCATE_KEEP_FRAC",
              "AIFORGE_SHELL_TIMEOUT", "AIFORGE_RISK_GATE_FAIL_OPEN"):
        monkeypatch.delenv(k, raising=False)
    yield tmp_path
    _fs._READ_CACHE.clear()
    _fs.reset_touched()


# ─── the touch tracker ─────────────────────────────────────────────────


def test_touches_are_recorded_repo_relative(repo):
    _fs.record_touch(str(repo / "app" / "store.py"))
    _fs.record_touch("app/cli.py")
    assert _fs.touched_paths() == ["app/cli.py", "app/store.py"]


def test_a_path_outside_the_root_is_still_recorded(repo):
    """Best-effort: the tracker is informational, so it must not raise into a
    tool call just because the path is odd."""
    _fs.record_touch("/etc/passwd")
    assert _fs.touched_paths() == ["etc/passwd"]


def test_empty_and_dot_paths_are_not_recorded(repo):
    _fs.record_touch("")
    _fs.record_touch(".")
    assert _fs.touched_paths() == []


def test_the_tracker_is_cleared_between_runs(repo):
    _fs.record_touch("a.py")
    _fs.reset_touched()
    assert _fs.touched_paths() == []


# ─── reading ───────────────────────────────────────────────────────────


def test_a_file_is_read_with_its_size(repo):
    (repo / "a.py").write_text("x = 1\n")
    assert _fs.file_read("a.py") == {"ok": True, "path": "a.py",
                                     "content": "x = 1\n", "bytes": 6}


def test_a_directory_is_not_a_file(repo):
    (repo / "d").mkdir()
    assert _fs.file_read("d")["error"].startswith("not a file")


def test_a_missing_file(repo):
    assert _fs.file_read("gone.py")["ok"] is False


def test_a_read_is_cached_until_the_file_changes(repo, monkeypatch):
    p = repo / "a.py"
    p.write_text("first\n")
    assert _fs.file_read("a.py")["content"] == "first\n"
    # same mtime → the cache answers even though the bytes changed underneath
    mt = p.stat().st_mtime_ns
    p.write_text("second\n")
    os.utime(p, ns=(mt, mt))
    assert _fs.file_read("a.py")["content"] == "first\n"
    # a real mtime bump misses the cache
    os.utime(p, ns=(mt + 1000, mt + 1000))
    assert _fs.file_read("a.py")["content"] == "second\n"


def test_the_read_cache_is_bounded(repo, monkeypatch):
    monkeypatch.setattr(_fs, "_READ_CACHE_MAX", 3)
    for i in range(5):
        (repo / f"f{i}.py").write_text("x\n")
        _fs.file_read(f"f{i}.py")
    assert len(_fs._READ_CACHE) <= 3


def test_a_traversing_read_is_refused(repo):
    assert _fs.file_read("../../etc/passwd")["ok"] is False


# ─── writing ───────────────────────────────────────────────────────────


def test_a_write_creates_parents_and_records_the_touch(repo):
    out = _fs.file_write("app/store.py", "x = 1\n")
    assert out == {"ok": True, "path": "app/store.py", "bytes": 6}
    assert (repo / "app/store.py").read_text() == "x = 1\n"
    assert _fs.touched_paths() == ["app/store.py"]


def test_a_draft_that_does_not_parse_never_reaches_disk(repo):
    out = _fs.file_write("a.py", "def (:\n")
    assert out["ok"] is False
    assert out["error"].startswith("syntax_invalid")
    assert out["hint"]
    assert not (repo / "a.py").exists()


def test_the_syntax_gate_can_be_bypassed(repo, monkeypatch):
    monkeypatch.setenv("AIFORGE_DOER_SKIP_SYNTAX", "1")
    assert _fs.file_write("a.py", "def (:\n")["ok"] is True


def test_a_rewrite_that_drops_most_of_a_file_is_refused(repo):
    """A local model "rewriting" a file it read often drops most of it — on a
    real repo that silently destroys code."""
    (repo / "big.py").write_text("".join(f"line{i} = {i}\n" for i in range(60)))
    out = _fs.file_write("big.py", "line0 = 0\n")
    assert out["ok"] is False
    assert "would shrink existing big.py from 61 to 2 lines" in out["error"]
    assert "file_patch" in out["hint"]
    assert (repo / "big.py").read_text().count("\n") == 60


def test_a_small_file_is_not_protected(repo):
    (repo / "small.py").write_text("a = 1\nb = 2\n")
    assert _fs.file_write("small.py", "a = 1\n")["ok"] is True


def test_a_modest_shrink_is_allowed(repo):
    (repo / "big.py").write_text("".join(f"l{i} = {i}\n" for i in range(60)))
    keep = "".join(f"l{i} = {i}\n" for i in range(40))
    assert _fs.file_write("big.py", keep)["ok"] is True


def test_a_new_file_is_never_a_truncation(repo):
    assert _fs.file_write("new.py", "x = 1\n")["ok"] is True


def test_an_intentional_full_rewrite_can_be_allowed(repo, monkeypatch):
    monkeypatch.setenv("AIFORGE_ALLOW_TRUNCATION", "1")
    (repo / "big.py").write_text("".join(f"l{i} = {i}\n" for i in range(60)))
    assert _fs.file_write("big.py", "l0 = 0\n")["ok"] is True


def test_the_truncation_thresholds_are_tunable(repo, monkeypatch):
    monkeypatch.setenv("AIFORGE_TRUNCATE_MIN_LINES", "3")
    monkeypatch.setenv("AIFORGE_TRUNCATE_KEEP_FRAC", "0.9")
    (repo / "s.py").write_text("a = 1\nb = 2\nc = 3\nd = 4\n")
    assert _fs.file_write("s.py", "a = 1\nb = 2\n")["ok"] is False


def test_junk_thresholds_fall_back_to_the_defaults(repo, monkeypatch):
    monkeypatch.setenv("AIFORGE_TRUNCATE_MIN_LINES", "lots")
    (repo / "big.py").write_text("".join(f"l{i} = {i}\n" for i in range(60)))
    assert _fs.file_write("big.py", "l0 = 0\n")["ok"] is False


def test_an_unreadable_existing_file_does_not_block_the_write(repo, monkeypatch):
    (repo / "a.py").write_text("x = 1\n")
    import pathlib
    monkeypatch.setattr(pathlib.Path, "read_text",
                        lambda self, **kw: (_ for _ in ()).throw(OSError("locked")))
    assert _fs._truncation_refusal(repo / "a.py", "a.py", "y = 2\n") is None


# ─── patching ──────────────────────────────────────────────────────────


def test_a_unique_match_is_replaced(repo):
    (repo / "a.py").write_text("x = 1\ny = 2\n")
    assert _fs.file_patch("a.py", "x = 1", "x = 99") == {
        "ok": True, "path": "a.py", "replaced": True}
    assert (repo / "a.py").read_text() == "x = 99\ny = 2\n"
    assert _fs.touched_paths() == ["a.py"]


def test_an_ambiguous_match_is_refused_with_the_count(repo):
    """More context is the caller's job — guessing which one would corrupt."""
    (repo / "a.py").write_text("v = 1\nv = 1\n")
    assert _fs.file_patch("a.py", "v = 1", "v = 2") == {
        "ok": False, "error": "ambiguous_match", "occurrences": 2}
    assert (repo / "a.py").read_text() == "v = 1\nv = 1\n"


def test_text_that_is_not_there(repo):
    (repo / "a.py").write_text("x = 1\n")
    assert _fs.file_patch("a.py", "zzz", "y")["error"] == "old_text_not_found"


def test_patching_a_missing_file(repo):
    assert _fs.file_patch("gone.py", "a", "b")["error"] == "not_found"


# ─── listing ───────────────────────────────────────────────────────────


def test_entries_are_listed_by_kind(repo):
    (repo / "d").mkdir()
    (repo / "f.py").write_text("x")
    out = _fs.list_dir("")
    assert out["ok"] is True
    assert out["path"] == "."
    assert out["entries"] == [{"name": "d", "kind": "dir"},
                              {"name": "f.py", "kind": "file"}]


def test_listing_a_file_is_an_error(repo):
    (repo / "f.py").write_text("x")
    assert _fs.list_dir("f.py")["error"].startswith("not a dir")


def test_listing_outside_the_root_is_refused(repo):
    assert _fs.list_dir("../..")["ok"] is False


# ─── the shell ─────────────────────────────────────────────────────────


@pytest.fixture
def risk(monkeypatch):
    from aiforge_core.runtime.tools import command_risk
    state = {"level": "safe", "reason": ""}
    monkeypatch.setattr(command_risk, "assess", lambda cmd: state)
    return state


def test_a_command_runs_in_the_repo_root(repo, risk):
    out = _fs.run_shell("pwd")
    assert out["ok"] is True
    assert out["returncode"] == 0
    assert str(repo) in out["stdout"]


def test_a_failing_command_reports_its_code_and_a_digest(repo, risk):
    out = _fs.run_shell("echo 'ImportError: boom' >&2; exit 3")
    assert out["ok"] is False
    assert out["returncode"] == 3
    assert "ImportError: boom" in out["digest"]


def test_a_dangerous_command_is_blocked(repo, risk, monkeypatch):
    from aiforge_core.runtime.tools import command_risk
    risk["level"] = command_risk.DANGEROUS
    risk["reason"] = "would wipe the disk"
    out = _fs.run_shell("rm -rf /")
    assert out == {"ok": False, "error": "blocked_dangerous_command",
                   "reason": "would wipe the disk", "returncode": -1}


def test_a_broken_safety_classifier_fails_closed(repo, monkeypatch):
    """A gate that errors must not become arbitrary execution."""
    from aiforge_core.runtime.tools import command_risk
    monkeypatch.setattr(command_risk, "assess",
                        lambda cmd: (_ for _ in ()).throw(RuntimeError("bad regex")))
    out = _fs.run_shell("echo hi")
    assert out["error"] == "risk_check_failed"
    assert "bad regex" in out["reason"]


def test_the_fail_closed_gate_can_be_overridden(repo, monkeypatch):
    from aiforge_core.runtime.tools import command_risk
    monkeypatch.setattr(command_risk, "assess",
                        lambda cmd: (_ for _ in ()).throw(RuntimeError("bad regex")))
    monkeypatch.setenv("AIFORGE_RISK_GATE_FAIL_OPEN", "1")
    assert _fs.run_shell("echo hi")["ok"] is True


def test_a_timeout_returns_what_was_captured(repo, risk, monkeypatch):
    def _boom(*_a, **_kw):
        raise subprocess.TimeoutExpired(cmd="x", timeout=1,
                                        output=b"partial out",
                                        stderr=b"Traceback (most recent call last):")
    monkeypatch.setattr(subprocess, "run", _boom)
    out = _fs.run_shell("sleep 999")
    assert out["ok"] is False
    assert out["error"] == "timeout"
    assert out["stdout"] == "partial out"
    assert "Traceback" in out["digest"]


def test_a_junk_timeout_value_falls_back(repo, risk, monkeypatch):
    monkeypatch.setenv("AIFORGE_SHELL_TIMEOUT", "soon")
    seen: dict = {}
    real = subprocess.run

    def _run(argv, **kw):
        seen["timeout"] = kw.get("timeout")
        return real(argv, **kw)
    monkeypatch.setattr(subprocess, "run", _run)
    _fs.run_shell("true")
    assert seen["timeout"] == 600


def test_output_is_truncated_per_stream(repo, risk):
    out = _fs.run_shell("python3 -c \"print('x' * 20000)\"")
    assert len(out["stdout"]) == 8000
    assert out["truncated"] is True


def test_a_clean_run_gets_no_digest(repo, risk):
    assert "digest" not in _fs.run_shell("echo fine")


def test_a_broken_compactor_never_breaks_a_tool(monkeypatch):
    import aiforge_core.runtime.output_compactor as oc
    monkeypatch.setattr(oc, "digest",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert _fs._compact_digest("out", "err", 1) == ""


# ─── grep ──────────────────────────────────────────────────────────────


def test_an_empty_pattern_is_refused(repo):
    assert _fs.grep_repo("   ")["error"] == "empty pattern"


def test_a_missing_search_root(repo):
    assert _fs.grep_repo("x", "nope")["error"].startswith("not found")


def test_hits_come_back_repo_relative(repo):
    (repo / "app").mkdir()
    (repo / "app/store.py").write_text("def get(k):\n    return k\n")
    out = _fs.grep_repo("def get", ".")
    assert out["ok"] is True
    assert out["hits"] == [{"file": "app/store.py", "line": 1, "text": "def get(k):"}]


def test_the_grep_command_excludes_vendor_dirs(monkeypatch, repo):
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name: None)
    cmd, rg = _fs._grep_command(repo, "pat")
    assert rg is False
    assert cmd[0] == "grep"
    assert "--exclude-dir=node_modules" in cmd


def test_ripgrep_is_used_when_present(monkeypatch, repo):
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/rg" if name == "rg" else None)
    cmd, rg = _fs._grep_command(repo, "pat")
    assert rg is True
    assert cmd[0] == "/usr/bin/rg"
    assert "!node_modules" in cmd


def test_a_colon_in_the_matched_line_stays_in_the_text(repo):
    hits = _fs._parse_grep_hits(f"{repo}/a.py:12:d = {{'k': 'v'}}\nnot a hit\n")
    assert hits == [{"file": "a.py", "line": 12, "text": "d = {'k': 'v'}"}]


def test_a_non_numeric_line_number_reads_as_zero(repo):
    assert _fs._parse_grep_hits("a.py:NaN:text")[0]["line"] == 0


def test_grep_output_is_capped(repo):
    rows = "".join(f"{repo}/a.py:{i}:hit\n" for i in range(1, 400))
    assert len(_fs._parse_grep_hits(rows)) == 200


def test_a_missing_grep_binary_is_reported(repo, monkeypatch):
    def _boom(*_a, **_kw):
        raise FileNotFoundError("rg")
    monkeypatch.setattr(subprocess, "run", _boom)
    assert "binary missing" in _fs.grep_repo("x")["error"]


def test_a_grep_timeout_is_reported(repo, monkeypatch):
    def _boom(*_a, **_kw):
        raise subprocess.TimeoutExpired(cmd="grep", timeout=30)
    monkeypatch.setattr(subprocess, "run", _boom)
    assert _fs.grep_repo("x")["error"] == "timeout"


# ─── line ranges ───────────────────────────────────────────────────────


def test_a_line_range_is_inclusive(repo):
    (repo / "a.py").write_text("".join(f"line{i}\n" for i in range(1, 11)))
    out = _fs.read_lines("a.py", 2, 4)
    assert out["text"] == "line2\nline3\nline4\n"
    assert out["total_lines"] == 10
    assert out["start"] == 2
    assert out["end"] == 4


def test_end_zero_reads_to_eof(repo):
    (repo / "a.py").write_text("a\nb\nc\n")
    assert _fs.read_lines("a.py", 2)["text"] == "b\nc\n"


def test_an_end_past_eof_is_clamped(repo):
    (repo / "a.py").write_text("a\nb\n")
    assert _fs.read_lines("a.py", 1, 999)["end"] == 2


def test_a_start_past_eof_says_so_rather_than_erroring(repo):
    (repo / "a.py").write_text("a\n")
    out = _fs.read_lines("a.py", 50)
    assert out["ok"] is True
    assert out["text"] == ""
    assert "past EOF" in out["note"]


def test_a_zero_start_is_clamped_to_the_first_line(repo):
    (repo / "a.py").write_text("a\nb\n")
    assert _fs.read_lines("a.py", 0, 1)["start"] == 1


def test_reading_a_missing_file(repo):
    assert _fs.read_lines("gone.py")["error"].startswith("not found")


def test_reading_a_directory_is_an_error(repo):
    (repo / "d").mkdir()
    assert _fs.read_lines("d")["ok"] is False
