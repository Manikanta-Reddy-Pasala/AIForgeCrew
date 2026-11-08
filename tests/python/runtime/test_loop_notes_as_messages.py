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
    src = inspect.getsource(_action)
    assert 'if _notes and not getattr(st, "native_on", False):' in src
