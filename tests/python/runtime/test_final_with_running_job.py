"""Two live misses on the Mac Studio with command check-ins.

* A 40-batch job was still running at "batch 29/40" and the model answered
  with that as the end of the output.
* A build printed ``error:`` at 3 s; the hand-back said only "command_wait to
  wait for more" and the model waited 45 s before killing it.
"""
from __future__ import annotations

import types

from aiforge_core.runtime import cmd_jobs
from aiforge_core.runtime.chat_agent._turn import _finish
from aiforge_core.runtime.cmd_signals import job_hint


def _st():
    return types.SimpleNamespace(convo=[], board_used=False, board={},
                                 readonly_mode=False, continue_nudges=0)


def _run(gen):
    events = []
    try:
        while True:
            events.append(next(gen))
    except StopIteration as stop:
        return events, stop.value


def test_an_answer_while_this_turns_job_runs_gets_one_nudge(monkeypatch):
    job = types.SimpleNamespace(key="bg-3")
    monkeypatch.setattr(cmd_jobs, "turn_running", lambda: [job])
    st = _st()
    step = {"text": "processing batch 29/40"}

    _events, sig = _run(_finish._final_nudges(st, step, "", False, []))
    assert sig == "continue"
    assert "bg-3" in st.convo[-1]["content"]
    assert "STILL RUNNING" in st.convo[-1]["content"]

    # Only once: the model may answer and say the job is still running.
    _events, sig = _run(_finish._final_nudges(st, step, "", False, []))
    assert sig is None


def test_no_running_job_no_nudge(monkeypatch):
    monkeypatch.setattr(cmd_jobs, "turn_running", lambda: [])
    st = _st()
    _events, sig = _run(_finish._final_nudges(st, {"text": "done"}, "", False, []))
    assert sig is None
    assert st.convo == []


def test_turn_running_is_empty_outside_a_turn():
    assert cmd_jobs.turn_running() == []


def test_an_error_hand_back_tells_the_model_to_kill_and_report():
    hint = job_hint("bg-1", True, "error:: error: undefined symbol 'x'")
    assert "command_kill(id='bg-1')" in hint
    assert "do not wait" in hint


def test_every_failure_kind_gets_the_kill_hint():
    from aiforge_core.runtime.cmd_signals import _FAILURES, failure_in
    for kind, _rx in _FAILURES:
        assert "command_kill" in job_hint("bg-1", True, f"{kind}: x")
    why = failure_in("compiling\nerror: undefined symbol 'parse_amount'\n")
    assert "do not wait" in job_hint("bg-1", True, why)


def test_a_stall_or_timeout_is_not_called_an_error():
    hint = job_hint("bg-1", True, "waited 15s; still working")
    assert "do not wait" not in hint


def test_a_plain_hand_back_still_offers_wait_peek_kill():
    hint = job_hint("bg-1", True, None)
    assert "command_wait(id='bg-1')" in hint
    assert "command_output(id='bg-1')" in hint


def test_a_finished_job_hint():
    assert job_hint("bg-1", False, "error") == "finished — the output above is final."


def test_the_failure_line_is_only_the_failing_line():
    from aiforge_core.runtime.cmd_signals import failure_in
    why = failure_in("compiling module 1\nerror: undefined symbol 'x'\nlinking")
    assert why.endswith("error: undefined symbol 'x'")
    assert "compiling" not in why
