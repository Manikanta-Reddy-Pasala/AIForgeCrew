"""A turn must not end on "I will report when it finishes" while its own job
runs, nor on a tool call written as text that cannot run."""
from __future__ import annotations

from types import SimpleNamespace

from aiforge_core.runtime.chat_agent._prompt import _parse
from aiforge_core.runtime.chat_agent._turn import _finish


def _drain(gen):
    out = []
    try:
        while True:
            out.append(next(gen))
    except StopIteration as stop:
        return out, stop.value


# ── a tool call written as text ──────────────────────────────────────────────

def test_a_cut_off_text_call_is_not_an_answer():
    text = ('Called file_patch({"path": "engine/src/main.rs", "old_text": "if let Some(max) '
            '= e2e_max {\\n info!(\\"engine: E2E mode')
    step = _parse(text)
    assert step["kind"] == "continue" and step["reason"] == "broken_call"
    assert step["tool"] == "file_patch"


def test_invalid_json_text_call_is_not_an_answer():
    step = _parse('Called run_command({"cmd": "ls", oops: 1})')
    assert step["kind"] == "continue" and step["reason"] == "broken_call"


def test_a_complete_text_call_still_runs():
    step = _parse('Called run_command({"cmd": "ls -la"})')
    assert step["kind"] == "action" and step["tool"] == "run_command"


def test_a_report_of_a_call_with_prose_after_it_is_still_an_answer():
    step = _parse('Called run_command({"cmd": "make"}) to build it, then the tests passed.')
    assert step["kind"] == "final"


def test_unknown_tool_text_is_left_alone():
    step = _parse('Called my_friend({"name": "x"')
    assert step["kind"] != "continue" or step.get("reason") != "broken_call"


def test_broken_call_nudge_says_it_did_not_run():
    st = SimpleNamespace(convo=[], continue_nudges=0)
    import aiforge_core.runtime.chat_agent._turn._finish as F
    import pytest
    mp = pytest.MonkeyPatch()
    mp.setattr(F, "detect", lambda *a, **k: None)          # no stuck signal here
    out, sig = _drain(F._handle_continue_step(
        st, {"kind": "continue", "reason": "broken_call", "tool": "file_patch"}, None, "/tmp"))
    assert sig == "continue"
    mp.undo()
    assert "did NOT run" in st.convo[-1]["content"] and "file_patch" in st.convo[-1]["content"]


# ── "I will report when it finishes" ─────────────────────────────────────────

class _Job:
    def __init__(self, key, turn=None):
        self.key = key
        self.turn = turn


def test_waiting_answer_with_a_live_job_is_sent_back_to_wait(monkeypatch):
    from aiforge_core.runtime import cmd_jobs
    monkeypatch.setattr(cmd_jobs, "running", lambda: [_Job("bg-1184")])
    st = SimpleNamespace(convo=[])
    step = {"text": "The rebuild is still running (bg-1184). I will report as soon as it finishes."}
    events, sig = _drain(_finish._wait_for_own_jobs(st, step))
    assert sig == "continue"
    assert 'command_wait {"id": "bg-1184"' in st.convo[-1]["content"]
    assert any("waiting for bg-1184" in e.get("text", "") for e in events)


def test_waiting_is_bounded(monkeypatch):
    from aiforge_core.runtime import cmd_jobs
    monkeypatch.setattr(cmd_jobs, "running", lambda: [_Job("bg-1")])
    st = SimpleNamespace(convo=[])
    step = {"text": "Build bg-1 is going; I'll let you know once it finishes."}
    sigs = [_drain(_finish._wait_for_own_jobs(st, step))[1] for _ in range(5)]
    assert sigs.count("continue") == _finish._BG_WAIT_NUDGES and sigs[-1] is None


def test_no_live_job_or_a_real_answer_ends_normally(monkeypatch):
    from aiforge_core.runtime import cmd_jobs
    monkeypatch.setattr(cmd_jobs, "running", lambda: [])
    st = SimpleNamespace(convo=[])
    assert _drain(_finish._wait_for_own_jobs(st, {"text": "I will report when it finishes."}))[1] is None
    monkeypatch.setattr(cmd_jobs, "running", lambda: [_Job("bg-2")])
    assert _drain(_finish._wait_for_own_jobs(st, {"text": "All 63 tests pass. Done."}))[1] is None


def test_a_running_server_or_an_old_job_is_not_waited_on(monkeypatch):
    from aiforge_core.runtime import cmd_jobs
    monkeypatch.setattr(cmd_jobs, "running", lambda: [_Job("pid-4242"), _Job("bg-7", turn=object())])
    st = SimpleNamespace(convo=[])
    step = {"text": "The dev server is up at http://localhost:3000; I will report back once you try it."}
    assert _drain(_finish._wait_for_own_jobs(st, step))[1] is None


def test_plan_mode_and_an_earlier_running_job_nudge_skip_the_wait(monkeypatch):
    from aiforge_core.runtime import cmd_jobs
    monkeypatch.setattr(cmd_jobs, "running", lambda: [_Job("bg-9")])
    step = {"text": "Build bg-9 is running; I will report back once it finishes."}
    assert _drain(_finish._wait_for_own_jobs(SimpleNamespace(convo=[], plan_mode=True), step))[1] is None
    assert _drain(_finish._wait_for_own_jobs(SimpleNamespace(convo=[], running_job_nudged=True), step))[1] is None


def test_ordinary_answers_do_not_trigger_the_wait(monkeypatch):
    from aiforge_core.runtime import cmd_jobs
    monkeypatch.setattr(cmd_jobs, "running", lambda: [_Job("bg-3")])
    for text in ("The server is still running at http://localhost:8080.",
                 "Next I will run the tests and check the output.",
                 "Plan: step 3 — once the build finishes, run pytest."):
        assert _drain(_finish._wait_for_own_jobs(SimpleNamespace(convo=[]), {"text": text}))[1] is None, text


def test_a_reported_call_with_an_explanation_is_an_answer():
    step = _parse("Called file_patch({...}) — it failed because the anchor text was missing.")
    assert step["kind"] == "final"


def test_the_reported_session_text_is_caught(monkeypatch):
    """The answer from the user's screenshot."""
    from aiforge_core.runtime import cmd_jobs
    monkeypatch.setattr(cmd_jobs, "running", lambda: [_Job("bg-1184")])
    text = ("The rebuild on the VM is still running (job bg-1184 — I'm polling the build "
            "status). The engine build takes a few minutes since it compiles the full Rust "
            "workspace. I will report as soon as it finishes, then run the E2E test.")
    assert _drain(_finish._wait_for_own_jobs(SimpleNamespace(convo=[]), {"text": text}))[1] == "continue"


# ── an answer that only announces the work ───────────────────────────────────

def test_an_answer_that_only_announces_the_work_is_sent_back():
    """The answer from the user's second screenshot."""
    text = ("I'm working through the E2E setup for the ai_elint pipeline — I need to fix "
            "the Rust code, rebuild it on the VM, and run the full test to verify it works "
            "end-to-end with proper table cleanup and timing reports at each layer.")
    st = SimpleNamespace(convo=[])
    events, sig = _drain(_finish._do_what_you_said(st, {"text": text}))
    assert sig == "continue" and "Do it now" in st.convo[-1]["content"]
    assert _drain(_finish._do_what_you_said(st, {"text": text}))[1] == "continue"
    assert _drain(_finish._do_what_you_said(st, {"text": text}))[1] is None   # bounded at 2


def test_real_answers_are_not_sent_back():
    for text in ("I fixed the timeout in engine/src/main.rs; all 12 tests pass.",
                 "I need your VM password to continue — which user should I use?",
                 "I'll explain: the cause is a race in the worker shutdown.",
                 "Here is the summary of the run.",
                 "The E2E run finished: 50000 rows, 4.2 s per layer.",
                 "I'll keep the current schema; no change needed.",
                 "I will use the existing helper.",
                 "Let me know which file and I'll patch it."):
        assert _drain(_finish._do_what_you_said(SimpleNamespace(convo=[]), {"text": text}))[1] is None, text


def test_plan_mode_keeps_its_plan():
    text = "I will first read the config, then I need to change the parser and run the tests."
    st = SimpleNamespace(convo=[], plan_mode=True)
    assert _drain(_finish._do_what_you_said(st, {"text": text}))[1] is None


def test_pipeline_builder_and_after_a_running_job_nudge_are_skipped():
    text = "I need to rebuild it on the VM and run the full test."
    assert _drain(_finish._do_what_you_said(SimpleNamespace(convo=[]), {"text": text}, None, True))[1] is None
    assert _drain(_finish._do_what_you_said(SimpleNamespace(convo=[]), {"text": text}, "skill"))[1] is None
    st = SimpleNamespace(convo=[], running_job_nudged=True)
    assert _drain(_finish._do_what_you_said(st, {"text": text}))[1] is None
    assert _drain(_finish._do_what_you_said(SimpleNamespace(convo=[]), {"text": text}))[1] == "continue"


def test_an_answer_that_ends_on_let_me_fix_it_is_sent_back():
    """The answer from the user's third screenshot."""
    text = ("The build script has a bug — it copies to `/tmp/buildsrc/` without creating "
            "that directory first. Let me fix it.")
    assert _drain(_finish._do_what_you_said(SimpleNamespace(convo=[]), {"text": text}))[1] == "continue"
    for ok in ("The build script had a bug; I fixed it and the build passes. Let me know if you want more.",
               "Fixed the path. Should I also push it?",
               "Done: the engine is rebuilt and the E2E run passed.",
               "The patch is ready. I'll push it if you want.",
               "Docs drafted. I'll update the index after you review.",
               "I'll continue once you approve."):
        assert _drain(_finish._do_what_you_said(SimpleNamespace(convo=[]), {"text": ok}))[1] is None, ok


def test_a_last_sentence_with_a_file_name_still_counts():
    text = "The copy step creates no folder. Let me fix build.sh."
    assert _drain(_finish._do_what_you_said(SimpleNamespace(convo=[]), {"text": text}))[1] == "continue"
