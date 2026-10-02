"""A team run that never reached its Doer must not end with research as the
answer.

Live bug: a user in Team mode asked for an implementation. The run ended with
the Researcher's brief ("Bottom line for the Planner: …") as the chat message
and nothing was changed — ``_promote_team_answer`` falls back to the
Researcher's text when there is no Doer output, and nothing checked that a
request which wanted changes had a Doer at all. Now the run names the stage it
stopped at, runs the Doer on the plan instead of ending, and only if that also
changes nothing says so plainly (with a stop marker so Retry resumes it).
"""
from __future__ import annotations

import asyncio
import queue
import subprocess
import types as pytypes

import pytest

from aiforge_core.runtime import chat_pipeline as P
from aiforge_core.runtime import chat_pipeline_turn as T

BRIEF = ("Bottom line for the Planner: the parallel architecture is ~80% built "
         "on disk … the real work is (a) … (b) … (c) …")
ASK = "implement a rust based parallel read path, target 500 ms for 50k records"


@pytest.fixture(autouse=True)
def unlocked():
    yield
    while P._RUN_LOCK.locked():
        try:
            P._RUN_LOCK.release()
        except RuntimeError:  # pragma: no cover
            break


class _Svc:
    def __init__(self, state=None):
        self._state = state or {}

    async def get_session(self, **_kw):
        return pytypes.SimpleNamespace(state=self._state)


def _outcome(by_role, *, state=None, prompt=ASK, **kw):
    q: queue.Queue = queue.Queue()
    out = asyncio.run(T._compute_team_outcome(
        _Svc(state), pytypes.SimpleNamespace(id="s"), by_role, "", None,
        "/repo", "base-sha", raw_prompt=prompt, q=q, steps=[], **kw))
    return out, q


# ─── detecting a run that never reached the Doer ───────────────────────


def test_a_researcher_only_run_for_a_change_request_is_stalled():
    assert T._stalled_before_doer({"researcher": BRIEF}, {}, None, ASK)


def test_a_run_with_a_doer_is_not_stalled():
    assert not T._stalled_before_doer({"doer": "added it"}, {}, None, ASK)
    assert not T._stalled_before_doer({}, {"doer_outcome": "added it"}, None, ASK)


def test_a_question_is_answered_by_whoever_answered():
    assert not T._stalled_before_doer({"researcher": "it works like…"}, {}, None,
                                      "how does the read path work?")


def test_an_enhancer_block_keeps_its_own_message():
    assert not T._stalled_before_doer({}, {}, "too vague", ASK)


def test_the_stage_reached_is_the_last_one_with_output():
    assert T._stage_reached({"enhancer": "x", "researcher": "y"}, {}) == "researcher"
    assert T._stage_reached({"researcher": "y"}, {"plan_md": "{}"}) == "planner"
    assert T._stage_reached({}, {}) == ""


# ─── what the user is told ─────────────────────────────────────────────


def test_research_is_never_presented_as_the_result(monkeypatch):
    monkeypatch.setenv("AIFORGE_TEAM_DOER_RECOVERY", "0")
    (msg, events, ok), _ = _outcome({"researcher": BRIEF})
    assert not ok and events == []
    assert msg.startswith("(stopped)")           # chat_persist's stop marker
    assert "Nothing was implemented" in msg
    assert "researcher stage" in msg
    assert BRIEF not in msg


def test_the_message_says_why_when_the_stream_failed(monkeypatch):
    monkeypatch.setenv("AIFORGE_TEAM_DOER_RECOVERY", "0")
    (msg, _e, ok), _ = _outcome({"planner": "{}"},
                                reason="TimeoutError: planner timed out")
    assert not ok
    assert "planner stage" in msg and "planner timed out" in msg


def test_a_request_for_prose_still_gets_the_researchers_answer():
    (msg, _e, ok), _ = _outcome({"researcher": "the auth flow works like…"},
                                prompt="explain the auth flow")
    assert ok and msg == "the auth flow works like…"


# ─── running the Doer on the plan instead of ending ────────────────────


def test_a_stalled_run_runs_the_doer_on_the_plan(monkeypatch):
    seen = {}

    def fake_doer(q, steps, cwd, session_id, brief):
        seen["brief"] = brief
        return "added the parallel reader"

    monkeypatch.setattr(T, "_run_doer_on_plan", fake_doer)
    monkeypatch.setattr(T, "_team_change_events",
                        lambda cwd, sha, blocked: [{"type": "changes"}])
    (msg, events, ok), q = _outcome({"researcher": BRIEF, "planner": "PLAN-X"},
                                    state={"plan_md": "PLAN-X"})
    assert ok and msg == "added the parallel reader"
    assert events == [{"type": "changes"}]
    assert ASK in seen["brief"] and "PLAN-X" in seen["brief"]
    assert "NOTHING has been changed yet" in seen["brief"]
    said = q.get_nowait()
    assert said["type"] == "thought" and "planner stage" in said["text"]
    assert "Running the implementation step" in said["text"]


def test_recovery_that_changes_nothing_ends_honestly(monkeypatch):
    monkeypatch.setattr(T, "_run_doer_on_plan",
                        lambda *a, **k: "I only reviewed the code")
    monkeypatch.setattr(T, "_team_change_events", lambda *a, **k: [])
    (msg, events, ok), _ = _outcome({"researcher": BRIEF},
                                    state={"plan_md": "PLAN-X"})
    assert not ok and events == []
    assert msg.startswith("(stopped)")
    assert "also made no file changes" in msg
    assert "I only reviewed the code" in msg
    assert "PLAN-X" in msg and "NOT the finished work" in msg


def test_a_crashing_recovery_is_reported_not_raised(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("model down")
    monkeypatch.setattr(T, "_run_doer_on_plan", boom)
    monkeypatch.setattr(T, "_team_change_events", lambda *a, **k: [])
    (msg, _e, ok), _ = _outcome({"researcher": BRIEF})
    assert not ok and "model down" in msg


def test_a_stopped_turn_is_not_re_run(monkeypatch):
    def never(*a, **k):
        raise AssertionError("the Doer must not run after a Stop")
    monkeypatch.setattr(T, "_run_doer_on_plan", never)
    (msg, _e, ok), _ = _outcome({"researcher": BRIEF}, recover=False,
                                reason="you stopped it")
    assert not ok and "you stopped it" in msg


def test_the_recovery_brief_labels_earlier_output_as_input():
    brief = T._recovery_brief(ASK, {"researcher": BRIEF}, {"plan_md": "P"},
                              "planner")
    assert brief.startswith(ASK)
    assert "Do not write a plan" in brief
    assert "Plan from the earlier stages:\nP" in brief
    assert "Research notes from the earlier stages:\n" + BRIEF in brief


# ─── through the real driver, with a stub ADK runner ───────────────────


def _ev(author, text):
    return pytypes.SimpleNamespace(
        author=author, partial=False,
        content=pytypes.SimpleNamespace(parts=[pytypes.SimpleNamespace(
            text=text, function_call=None, function_response=None)]),
        node_info=None, actions=None)


@pytest.fixture
def stub_runner(monkeypatch, tmp_path):
    import google.adk.runners as runners

    import aiforge_core.runtime.pipeline as pl
    monkeypatch.setattr(pl, "build_pipeline", lambda **kw: object())
    plan = {"events": [], "raise": None}

    class _Runner:
        def __init__(self, **kw):
            pass

        def run_async(self, **kw):
            async def gen():
                for e in plan["events"]:
                    yield e
                if plan["raise"]:
                    raise plan["raise"]
            return gen()

        async def close(self):
            pass

    monkeypatch.setattr(runners, "Runner", _Runner)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    return plan


def _run_driver(tmp_path):
    q: queue.Queue = queue.Queue()
    P._run_async_in_thread(lambda: P._drive(
        q, None, str(tmp_path), ASK, 0.0, ASK, {}))
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return [e for e in out if isinstance(e, dict)]


def _writing_doer(tmp_path):
    def doer(q, steps, cwd, session_id, brief):
        (tmp_path / "reader.py").write_text("print('parallel')\n")
        return "implemented reader.py"
    return doer


def test_driver_runs_the_doer_when_the_graph_ends_after_research(
        stub_runner, tmp_path, monkeypatch):
    stub_runner["events"] = [_ev("researcher", BRIEF)]
    monkeypatch.setattr(T, "_run_doer_on_plan", _writing_doer(tmp_path))
    events = _run_driver(tmp_path)
    msgs = [e["text"] for e in events if e.get("type") == "message"]
    assert msgs == ["implemented reader.py"]
    assert not any(e.get("type") == "stopped" for e in events)
    assert any(e.get("type") == "thought" and "implementation step" in
               e.get("text", "") for e in events)


def test_driver_runs_the_doer_when_the_stream_raises_after_research(
        stub_runner, tmp_path, monkeypatch):
    stub_runner["events"] = [_ev("researcher", BRIEF)]
    stub_runner["raise"] = RuntimeError("planner node failed")
    monkeypatch.setattr(T, "_run_doer_on_plan", _writing_doer(tmp_path))
    events = _run_driver(tmp_path)
    assert [e["text"] for e in events if e.get("type") == "message"] == [
        "implemented reader.py"]
    assert not any(e.get("reason") == "pipeline_error" for e in events)


def test_driver_never_shows_the_brief_as_the_answer(stub_runner, tmp_path,
                                                    monkeypatch):
    stub_runner["events"] = [_ev("researcher", BRIEF)]
    stub_runner["raise"] = RuntimeError("planner node failed")
    monkeypatch.setattr(T, "_run_doer_on_plan", lambda *a, **k: "no luck")
    events = _run_driver(tmp_path)
    msgs = [e["text"] for e in events if e.get("type") == "message"]
    assert len(msgs) == 1 and msgs[0].startswith("(stopped)")
    assert "planner node failed" in msgs[0] and "researcher stage" in msgs[0]
    assert any(e.get("type") == "stopped" for e in events)


def test_a_failure_after_the_doer_keeps_the_old_behaviour(stub_runner, tmp_path,
                                                          monkeypatch):
    stub_runner["events"] = [_ev("doer", "edited things")]
    stub_runner["raise"] = RuntimeError("feedback node failed")

    def never(*a, **k):
        raise AssertionError("the Doer already ran")
    monkeypatch.setattr(T, "_run_doer_on_plan", never)
    events = _run_driver(tmp_path)
    assert not [e for e in events if e.get("type") == "message"]
    assert any(e.get("reason") == "pipeline_error" for e in events)
