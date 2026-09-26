"""What the model READS for a command handed back at a check-in.

Live on the Mac Studio a build that printed ``error:`` at 3 s and kept
linking was handed back as ``{ok: True, running: True, new_output: ...}``;
the renderer called that ``exit 0`` and dropped the output, and the model
answered "The build completed successfully (exit code 0)".
"""
from __future__ import annotations

from aiforge_core.runtime.chat_agent._obs_text import render_observation


def _running(**extra):
    res = {"ok": True, "id": "bg-7", "running": True, "elapsed_s": 3.2,
           "new_output": "compiling module 1\n[stderr]\nerror: undefined symbol",
           "output_growing": True, "cpu_active": False,
           "returned_because": "error in output",
           "hint": "command_wait(id='bg-7') to keep waiting"}
    res.update(extra)
    return res


def test_a_running_job_is_never_shown_as_exit_0():
    text = render_observation("run_command", _running(), 4000)
    assert "exit 0" not in text
    assert "STILL RUNNING" in text and "bg-7" in text
    assert "error: undefined symbol" in text
    assert "returned early: error in output" in text
    assert "command_wait(id='bg-7')" in text


def test_a_finished_look_shows_its_real_exit_code_and_output():
    res = {"ok": False, "id": "bg-7", "running": False, "elapsed_s": 41.0,
           "code": 2, "new_output": "linking step 40\nFAILED",
           "hint": "finished"}
    text = render_observation("command_wait", res, 4000)
    assert "FINISHED" in text and "exit 2" in text
    assert "FAILED" in text


def test_a_stuck_job_says_so():
    text = render_observation("command_wait", _running(stuck=True), 4000)
    assert "looks stuck" in text


def test_an_unknown_job_id_is_not_rendered_as_running():
    res = {"ok": False, "error": "no such job", "running": ["bg-1"],
           "hint": "it may have finished"}
    text = render_observation("command_wait", res, 4000)
    assert "STILL RUNNING" not in text
    assert "no such job" in text


def test_a_plain_command_result_is_unchanged():
    text = render_observation("run_command",
                              {"ok": True, "code": 0, "stdout": "hi"}, 4000)
    assert text.startswith("exit 0")
    assert "hi" in text


def test_the_cap_holds():
    res = _running(new_output="x" * 50_000)
    assert len(render_observation("run_command", res, 800)) <= 800
