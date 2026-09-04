"""Running a command, and writing a file, from a chat turn.

The pre-flight gates all fail CLOSED, and each one is a bug that reached a
user: a foreground server start never returns, so run_command polled it until
the ten-minute timeout and wedged the turn (the "network error" report); a
blanket ``git add -A`` in chat swept the user's unrelated files into a commit;
a literal ``bash missing.sh`` came back as the shell's cryptic "No such file
or directory", which the model then thrashed on.

Around the process itself: it gets its own process group so Stop can kill the
whole tree, a timeout keeps whatever it buffered (which tests ran before the
hang is the useful signal, and the message says explicitly not to undo the
edits over it), and the output drain is bounded because a daemon grandchild
inheriting the stdout pipe keeps it open after the process exits.

Writes are syntax-checked before they land, so a broken edit is refused rather
than saved — with ``force`` as the deliberate escape.
"""
from __future__ import annotations

import subprocess
import types as pytypes

import pytest

from aiforge_core.runtime.chat_agent import _shell as S


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "_workspace_root", lambda: tmp_path)
    (tmp_path / "app.py").write_text("x = 1\n")
    return tmp_path


# ─── writing ───────────────────────────────────────────────────────────


def test_a_file_is_written_and_its_parents_created(repo):
    res = S._t_file_write({"path": "pkg/new.py", "content": "y = 2\n"},
                          str(repo))
    assert res["ok"] is True
    assert res["bytes"] == 6
    assert (repo / "pkg" / "new.py").read_text() == "y = 2\n"


def test_a_broken_file_is_refused_before_it_lands(repo):
    res = S._t_file_write({"path": "bad.py", "content": "def f(:\n"}, str(repo))
    assert res["error"] == "syntax_invalid"
    assert "force:true" in res["hint"]
    assert not (repo / "bad.py").exists()


def test_force_writes_it_anyway(repo):
    assert S._t_file_write({"path": "bad.py", "content": "def f(:\n",
                            "force": True}, str(repo))["ok"] is True


def test_an_empty_file_is_not_syntax_checked(repo):
    assert S._syntax_check("a.py", "   ", {}) is None


def test_a_non_code_file_is_not_syntax_checked(repo):
    assert S._syntax_check("notes.md", "def f(:", {}) is None


def test_a_guard_that_blows_up_never_blocks_a_write(repo, monkeypatch):
    from aiforge_core.runtime import syntax_guard
    monkeypatch.setattr(syntax_guard, "validate_syntax",
                        lambda p, c: (_ for _ in ()).throw(RuntimeError("x")))
    assert S._syntax_check("a.py", "def f(:", {}) is None


# ─── patching ──────────────────────────────────────────────────────────


def test_a_unique_snippet_is_replaced(repo):
    res = S._t_file_patch({"path": "app.py", "old_text": "x = 1",
                           "new_text": "x = 2"}, str(repo))
    assert res["ok"] is True
    assert (repo / "app.py").read_text() == "x = 2\n"


def test_a_snippet_that_is_not_there_is_reported(repo):
    assert S._t_file_patch({"path": "app.py", "old_text": "zzz",
                            "new_text": "q"}, str(repo))["error"] \
        == "old_text_not_found"


def test_an_ambiguous_snippet_is_refused_with_its_count(repo):
    (repo / "app.py").write_text("a = 1\na = 1\n")
    res = S._t_file_patch({"path": "app.py", "old_text": "a = 1",
                           "new_text": "b = 1"}, str(repo))
    assert res["error"] == "ambiguous_match"
    assert res["occurrences"] == 2


def test_a_patch_that_would_break_the_file_is_refused(repo):
    res = S._t_file_patch({"path": "app.py", "old_text": "x = 1",
                           "new_text": "def f(:"}, str(repo))
    assert res["error"] == "syntax_invalid"
    assert (repo / "app.py").read_text() == "x = 1\n", "left untouched"


def test_patching_a_file_that_is_not_there(repo):
    assert S._t_file_patch({"path": "ghost.py", "old_text": "a",
                            "new_text": "b"}, str(repo))["error"] == "not_found"


# ─── listing ───────────────────────────────────────────────────────────


def test_a_directory_lists_with_folders_marked(repo):
    (repo / "src").mkdir()
    entries = S._t_list_dir({"path": "."}, str(repo))["entries"]
    assert "src/" in entries
    assert "app.py" in entries


def test_listing_something_that_is_not_a_directory(repo):
    assert S._t_list_dir({"path": "app.py"}, str(repo))["ok"] is False


# ─── the pre-flight gates ──────────────────────────────────────────────


@pytest.fixture
def gates(monkeypatch):
    from aiforge_core.runtime.tools import delete_guard
    state: dict = {"allow_delete": False, "destructive": False}
    monkeypatch.setattr(delete_guard, "allow_delete",
                        lambda envs=None: state["allow_delete"])
    monkeypatch.setattr(delete_guard, "is_destructive_delete",
                        lambda cmd: state["destructive"])
    return state


def test_an_ordinary_command_passes_every_gate(gates, repo):
    assert S._run_refusal("pytest -q", {}, str(repo)) is None


def test_a_destructive_delete_needs_the_user_to_agree(gates, repo):
    gates["destructive"] = True
    res = S._run_refusal("rm -rf build", {}, str(repo))
    assert res["blocked"] == "delete"
    assert "confirm_delete=true" in res["error"]


def test_an_agreed_delete_runs(gates, repo):
    gates["destructive"] = True
    assert S._run_refusal("rm -rf build", {"confirm_delete": True},
                          str(repo)) is None


def test_deletes_can_be_allowed_wholesale(gates, repo):
    gates["destructive"] = True
    gates["allow_delete"] = True
    assert S._run_refusal("rm -rf build", {}, str(repo)) is None


@pytest.mark.parametrize("cmd", ["git add -A", "git add .",
                                 "git commit -am 'wip'"])
def test_blanket_staging_is_refused_so_it_can_be_re_issued(gates, repo, cmd):
    """In chat it would sweep the user's unrelated files into a commit."""
    res = S._run_refusal(cmd, {}, str(repo))
    assert res["blocked"] == "blanket_git"


def test_targeted_staging_is_fine(gates, repo):
    assert S._run_refusal("git add src/app.py", {}, str(repo)) is None
    assert S._run_refusal("git commit -m 'msg'", {}, str(repo)) is None


@pytest.mark.parametrize("cmd", ["npm run dev", "python -m http.server",
                                 "uvicorn app:main --reload"])
def test_a_foreground_server_is_redirected_to_serve(gates, repo, cmd):
    """It never returns, so run_command polled it to the timeout and wedged
    the turn."""
    assert S._run_refusal(cmd, {}, str(repo))["blocked"] == "server_start"


def test_a_missing_script_is_named_instead_of_shell_noise(gates, repo):
    res = S._run_refusal("bash deploy.sh", {}, str(repo))
    assert res["blocked"] == "missing_path"
    assert "deploy.sh" in res["error"]


def test_a_script_that_exists_runs(gates, repo):
    (repo / "deploy.sh").write_text("echo hi\n")
    assert S._run_refusal("bash deploy.sh", {}, str(repo)) is None


def test_a_missing_directory_in_a_cd_is_named(gates, repo):
    res = S._run_refusal("cd nope && ls", {}, str(repo))
    assert res["blocked"] == "missing_path"


# ─── running it ────────────────────────────────────────────────────────


class _Proc:
    def __init__(self, polls=1, rc=0, out="ok", err="", hang=False):
        self.pid = 4321
        self._polls = polls
        self._rc = rc
        self.returncode = None
        self._out, self._err = out, err
        self._hang = hang
        self.communications = 0

    def poll(self):
        self._polls -= 1
        if self._polls <= 0:
            self.returncode = self._rc
            return self._rc
        return None

    def communicate(self, timeout=None):
        self.communications += 1
        if self._hang and self.communications == 1:
            raise subprocess.TimeoutExpired("cmd", timeout or 0)
        return self._out, self._err

    def kill(self):
        self.returncode = -9


@pytest.fixture
def run(monkeypatch, repo, gates):
    from aiforge_core.runtime import chat_cancel
    state: dict = {"proc": _Proc(), "sid": None, "cancelled": False,
                   "killed": [], "pgids": [], "spawn": None}
    monkeypatch.setattr(S.subprocess, "Popen",
                        lambda cmd, **kw: state.update(spawn=(cmd, kw))
                        or state["proc"])
    monkeypatch.setattr(S.os, "getpgid", lambda pid: 999)
    from aiforge_core.runtime import proc_signals as _ps
    monkeypatch.setattr(_ps.os, "killpg",
                        lambda pgid, sig: state["killed"].append(sig))
    monkeypatch.setattr(chat_cancel, "active", lambda: state["sid"])
    monkeypatch.setattr(chat_cancel, "is_cancelled",
                        lambda sid: state["cancelled"])
    monkeypatch.setattr(chat_cancel, "track_pgid",
                        lambda sid, pgid: state["pgids"].append((sid, pgid)))
    import time
    monkeypatch.setattr(time, "sleep", lambda s: None)
    return state


def test_a_command_runs_in_its_own_process_group(run, repo):
    """So Stop can kill the shell AND its children."""
    res = S._t_run_command({"cmd": "pytest -q"}, str(repo))
    assert res == {"ok": True, "code": 0, "stdout": "ok", "stderr": ""}
    assert run["spawn"][1]["start_new_session"] is True
    assert run["spawn"][1]["cwd"] == str(repo)


def test_a_failing_command_reports_its_code(run, repo):
    run["proc"] = _Proc(rc=1, out="", err="2 failed")
    res = S._t_run_command({"cmd": "pytest"}, str(repo))
    assert res["ok"] is False
    assert res["code"] == 1
    assert res["stderr"] == "2 failed"


def test_the_group_is_registered_so_stop_can_find_it(run, repo):
    run["sid"] = 7
    S._t_run_command({"cmd": "pytest"}, str(repo))
    assert run["pgids"] == [(7, 999)]


def test_stop_kills_the_tree_mid_run(run, repo):
    run["sid"] = 7
    run["cancelled"] = True
    run["proc"] = _Proc(polls=5)
    res = S._t_run_command({"cmd": "sleep 100"}, str(repo))
    assert res["stopped"] is True
    assert run["killed"]


def test_a_timeout_keeps_the_partial_output_and_says_not_to_undo(run, repo,
                                                                 monkeypatch):
    """Which tests ran before the hang is the signal the agent needs."""
    import time
    run["proc"] = _Proc(polls=99, out="3 passed", err="")
    ticks = iter([0.0, 100.0, 100.0])
    monkeypatch.setattr(time, "monotonic", lambda: next(ticks))
    res = S._t_run_command({"cmd": "pytest", "timeout": 1}, str(repo))
    assert res["timed_out"] is True
    assert res["stdout"] == "3 passed"
    assert "Do NOT undo your edits" in res["error"]
    assert "NARROWER" in res["error"]


def test_a_timed_out_process_that_will_not_drain_is_killed(run):
    class _Stuck(_Proc):
        def communicate(self, timeout=None):
            raise subprocess.TimeoutExpired("cmd", 5)
    res = S._timeout_result(_Stuck(), 30)
    assert res["stdout"] == ""
    assert res["timed_out"] is True
    assert run["killed"]


def test_a_daemon_grandchild_cannot_block_the_drain_forever(run):
    """`npm run dev &` inherits the stdout pipe and keeps it open after the
    process exits."""
    proc = _Proc(hang=True, out="partial")
    assert S._collect_output(proc) == ("partial", "")
    assert run["killed"]


def test_the_drain_budget_is_tunable(run, monkeypatch, repo):
    monkeypatch.setenv("AIFORGE_COMMUNICATE_TIMEOUT_S", "not-a-number")
    assert S._collect_output(_Proc()) == ("ok", "")


def test_a_spawn_failure_is_a_soft_error(run, repo, monkeypatch):
    monkeypatch.setattr(S.subprocess, "Popen",
                        lambda cmd, **kw: (_ for _ in ()).throw(
                            OSError("fork failed")))
    assert S._t_run_command({"cmd": "ls"}, str(repo)) == {
        "ok": False, "error": "fork failed"}


def test_a_refused_command_never_spawns(run, repo, gates, monkeypatch):
    gates["destructive"] = True
    monkeypatch.setattr(S.subprocess, "Popen",
                        lambda *a, **k: pytest.fail("ran a refused command"))
    assert S._t_run_command({"cmd": "rm -rf /"}, str(repo))["blocked"] == "delete"


def test_a_killed_process_is_reaped(run):
    proc = _Proc()
    S._kill_proc(proc)
    assert proc.communications == 1, "no zombie, no leaked pipe fds"


def test_a_process_that_cannot_be_signalled_is_killed_directly(run,
                                                               monkeypatch):
    from aiforge_core.runtime import proc_signals as _ps
    monkeypatch.setattr(_ps.os, "killpg",
                        lambda pgid, sig: (_ for _ in ()).throw(OSError("gone")))
    proc = _Proc()
    S._kill_proc(proc)
    assert proc.returncode == -9


def test_a_drain_that_fails_reports_nothing_rather_than_raising():
    class _Broken(_Proc):
        def communicate(self, timeout=None):
            raise OSError("closed")
    assert S._drain(_Broken()) is None


# ─── reading files back ────────────────────────────────────────────────


def test_a_file_is_read(repo):
    assert S._t_file_read({"path": "app.py"}, str(repo))["content"] == "x = 1\n"


def test_several_files_are_read_in_one_call(repo):
    """A local model on a long one-at-a-time read chain loses track and stalls
    re-reading files it already has."""
    (repo / "b.py").write_text("y = 2\n")
    out = S._t_read_files({"paths": ["app.py", "b.py"]}, str(repo))
    assert out["read"] == 2
    assert out["note"] == "2 read, 0 failed"
    assert "=== app.py ===" in out["content"]
    assert "y = 2" in out["content"]


def test_a_missing_file_in_a_batch_is_reported_not_fatal(repo):
    out = S._t_read_files({"paths": ["app.py", "ghost.py"]}, str(repo))
    assert out["ok"] is True
    assert out["failed"] == 1
    assert "x = 1" in out["content"]
    assert "[read failed:" in out["content"]


def test_a_batch_with_no_paths_is_refused(repo):
    assert "missing 'paths'" in S._t_read_files({}, str(repo))["error"]


def test_a_huge_batch_is_capped_and_says_to_call_again(repo):
    out = S._t_read_files({"paths": [f"f{i}.py" for i in range(70)]},
                          str(repo))
    assert "10 skipped" in out["note"]


@pytest.mark.parametrize("args,expected", [
    ({"paths": ["a", "b"]}, ["a", "b"]),
    ({"paths": "a, b"}, ["a", "b"]),
    ({"path": "a"}, ["a"]),
    ({}, []),
])
def test_the_requested_paths_are_read_off_any_shape(args, expected):
    assert S._requested_paths(args) == expected


def test_no_single_file_can_eat_the_observation_budget(repo, monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_READ_FILES_PER_CAP", "200")
    (repo / "big.py").write_text("z" * 900)
    out = S._t_read_files({"paths": ["big.py"]}, str(repo))
    assert "truncated 700 chars" in out["content"]
    assert "read_lines" in out["content"], "and how to get the rest"


@pytest.mark.parametrize("value,expected", [("lots", 6000), ("50", 200),
                                            ("9000", 9000)])
def test_the_per_file_cap_has_a_floor_and_a_default(monkeypatch, value,
                                                    expected):
    monkeypatch.setenv("AIFORGE_CHAT_READ_FILES_PER_CAP", value)
    assert S._read_files_cap() == expected
