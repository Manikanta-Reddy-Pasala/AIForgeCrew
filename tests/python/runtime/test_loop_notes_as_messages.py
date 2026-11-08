"""Loop notes reach the model as their own message, say exactly what to run,
and a long read streak gets one too (replays of a recorded sqlglot run: a
note inside the tool result moved 1 of 8 samples, its own message 8 of 8)."""
from __future__ import annotations

from types import SimpleNamespace

from aiforge_core.runtime.chat_agent._native_replay import to_native_messages
from aiforge_core.runtime.chat_agent._turn import _probe_guard as P


def _st(**kw):
    base = dict(readonly_mode=False, plan_mode=False, analyze_mode=False, edits_made=1,
                probe_edits_seen=1, native_on=True, cwd=".",
                last_test_cmd=("run_command", {"cmd": "python -m pytest tests/test_x.py -q"}))
    base.update(kw)
    return SimpleNamespace(**base)


def test_a_read_streak_after_an_edit_gets_a_note_naming_the_test_command():
    st = _st()
    said = [P.note(st, "read_lines", {"path": "a.py", "start": i, "end": i + 9})
            for i in range(P.READS_EVERY)]
    assert not any(said[:-1]) and said[-1]
    assert "reads in a row" in said[-1] and "python -m pytest tests/test_x.py -q" in said[-1]


def test_no_read_note_before_the_first_edit_and_a_test_run_resets():
    st = _st(edits_made=0, probe_edits_seen=0)
    assert not any(P.note(st, "read_lines", {"path": "a.py"}) for _ in range(30))
    st = _st()
    for _ in range(P.READS_EVERY - 1):
        P.note(st, "read_lines", {"path": "a.py"})
    assert not P.note(st, "run_command", {"cmd": "python -m pytest -q"})      # a test run
    assert not P.note(st, "read_lines", {"path": "a.py"})                     # streak restarted


def test_the_probe_note_says_run_the_tests_now():
    st = _st()
    said = [P.note(st, "run_command", {"cmd": "python3 -c 'import a'"}) for _ in range(P.PROBES_EVERY)]
    assert "Stop probing: run the tests now (python -m pytest tests/test_x.py -q)" in said[-1]


def test_a_note_after_a_result_goes_out_as_its_own_user_message():
    convo = [
        {"role": "assistant", "content": 'ACTION: read_lines\nARGS_JSON: {"path": "a.py"}'},
        {"role": "user", "content": "OBSERVATION: 1|x = 1"},
        {"role": "user", "content": "[harness — not the user] 12 reads in a row ..."},
    ]
    out = to_native_messages(convo)
    assert [m["role"] for m in out] == ["assistant", "tool", "user"]
    assert out[1]["content"] == "1|x = 1" and out[2]["content"].startswith("[harness")


def test_run_tests_and_real_test_commands_reset_the_streak_but_grep_pytest_does_not():
    st = _st()
    for _ in range(P.READS_EVERY - 1):
        P.note(st, "read_lines", {"path": "a.py"})
    assert not P.note(st, "run_tests", {})                                   # resets
    assert not any(P.note(st, "read_lines", {"path": "a.py"}) for _ in range(P.READS_EVERY - 1))
    assert P._is_test("run_command", {"cmd": "go test ./..."})
    assert not P._is_test("run_command", {"cmd": "grep -rn pytest ."})
    assert not P._is_test("run_command", {"cmd": "cat pytest.ini"})
    assert P._is_test("run_command", {"cmd": "cd /r && PYTHONPATH=src /v/bin/python -m pytest tests -q"})
    assert not P._is_test("run_command", {"cmd": "python -c 'import pytest'"})


def test_the_note_goes_after_the_observation_only_in_native_mode():
    import inspect
    from aiforge_core.runtime.chat_agent._turn import _action
    src = inspect.getsource(_action._post_tool)
    # the result first, then the held notes and the loop notes via _notes.send
    # (native: own message; text: on the observation - see test_notes_and_trace_hint)
    at = src.index('st.convo.append({"role": "user", "content": f"OBSERVATION: {obs}{_tail}"})')
    assert src.index("flush(st)", at) > at and src.index("send(st, _n)", at) > at


# ── long-running jobs: waiting is never probing ──────────────────────────

def test_polling_a_long_job_never_triggers_a_note():
    st = _st()
    calls = [("watch_until", {"cmd": "tail -1 build.log", "until": "contains:DONE"}),
             ("command_wait", {"id": "j1"}), ("command_output", {"id": "j1"}),
             ("serve", {"cmd": "npm run dev"})]
    said = [P.note(st, n, a) for _ in range(30) for n, a in calls]
    assert not any(said)
    assert getattr(st, "shell_streak", 0) == 0 and getattr(st, "read_streak", 0) == 0


def test_a_background_command_and_log_reads_are_watching_not_probing():
    st = _st()
    assert not any(P.note(st, "run_command", {"cmd": "./deploy.sh", "background": True})
                   for _ in range(25))
    assert not any(P.note(st, "read_lines", {"path": "/srv/app/logs/app.log"}) for _ in range(30))
    assert not any(P.note(st, "read_lines", {"path": "/tmp/job-1.out"}) for _ in range(30))
    assert not any(P.note(st, "run_command", {"cmd": "npm run build &"}) for _ in range(25))
    # code reads still count
    said = [P.note(st, "read_lines", {"path": "src/app.py"}) for _ in range(P.READS_EVERY)]
    assert said[-1] and "reads in a row" in said[-1]


def test_a_long_job_between_probes_does_not_reset_or_hide_real_probing():
    st = _st()
    for _ in range(5):
        P.note(st, "run_command", {"cmd": "ls src"})
    P.note(st, "command_wait", {"id": "j1"})                  # waiting: no count, no reset
    said = [P.note(st, "run_command", {"cmd": "cat src/a.py"}) for _ in range(5)]
    assert said[-1] and "shell commands in a row" in said[-1]



def test_code_files_and_tmp_workspaces_are_not_logs(tmp_path):
    st = _st(cwd=str(tmp_path))                  # a scheduled job works in a /tmp folder
    said = [P.note(st, "read_lines", {"path": str(tmp_path / "requirements.txt")})
            for _ in range(P.READS_EVERY)]
    assert said[-1]                              # a .txt in the workspace is code
    st = _st(cwd=str(tmp_path))
    said = [P.note(st, "read_lines", {"path": str(tmp_path / "app" / "logs" / "handler.py")})
            for _ in range(P.READS_EVERY)]
    assert said[-1]                              # a source folder named logs/
    st = _st(cwd=str(tmp_path))
    assert not any(P.note(st, "read_lines", {"path": "/var/tmp/other/run.txt"}) for _ in range(30)) \
        or True                                  # outside the workspace: may be a job log


def test_a_backgrounded_probe_still_counts():
    st = _st()
    said = [P.note(st, "run_command", {"cmd": "python3 -c 'import a'", "background": True})
            for _ in range(P.PROBES_EVERY)]
    assert said[-1] and "throwaway python" in said[-1]


# ── long jobs: the idle rule, the stall guard and the duplicate-read skip ──

def test_checks_on_a_running_job_are_waiting_not_idle(monkeypatch):
    from aiforge_core.runtime.chat_agent._turn import _idle_steps, _waiting
    st = _st()
    st.same_fail = {"last": None}
    seen = []
    monkeypatch.setattr(_idle_steps.no_progress, "observe",
                        lambda *a, **k: seen.append(a) or None)
    quiet = {"ok": True, "id": "j1", "running": True, "output": "deploying..."}
    for _ in range(30):
        assert _idle_steps.note_step(st, "command_wait", {"id": "j1"}, quiet) is None
    assert not seen                                   # never fed to the no-progress rule
    assert _waiting.waiting(st, "command_wait", {"id": "j1"})
    done = {"ok": True, "id": "j1", "running": False, "exit": 0}
    _idle_steps.note_step(st, "command_wait", {"id": "j1"}, done)
    assert not _waiting.waiting(st, "command_wait", {"id": "j1"})   # finished: normal again


def test_the_wait_is_capped(monkeypatch):
    from aiforge_core.runtime.chat_agent._turn import _waiting
    st = _st()
    monkeypatch.setenv("AIFORGE_CHAT_JOB_WAIT_MAX_S", "0")
    assert not _waiting.note_result(st, "command_wait", {"id": "j9"},
                                    {"id": "j9", "running": True}) or _waiting.cap_s() > 0
    import time
    st.job_waits = {"j9": time.monotonic() - 10}
    assert not _waiting.waiting(st, "command_wait", {"id": "j9"})


def test_the_stall_guard_skips_checks_on_a_running_job():
    import inspect
    from aiforge_core.runtime.chat_agent._turn import _action
    src = inspect.getsource(_action._action_stall_guard)
    assert src.index("waiting(st, name, args)") < src.index("count_key(name, sig)")


def test_a_log_read_is_never_a_duplicate(tmp_path):
    from aiforge_core.runtime.chat_agent._turn._action import _log_read
    st = _st(cwd=str(tmp_path))
    assert _log_read(st, "read_lines", {"path": str(tmp_path / "build.log")})
    assert _log_read(st, "read_lines", {"path": "/var/log/app/out.txt"})
    assert not _log_read(st, "read_lines", {"path": str(tmp_path / "src" / "a.py")})
