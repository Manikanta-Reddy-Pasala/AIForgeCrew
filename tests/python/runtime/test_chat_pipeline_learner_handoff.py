"""A team turn answers before its Learner runs.

The Learner is the last node of the team graph and distils facts for memory —
nothing it says reaches the answer. Waiting for it (one LLM call plus its
markdown/OKR/SQLite writes) held the answer and the turn's end back by 5–60 s.
Once the validator gate routes ``done`` the driver posts the answer, persists
the turn and hands the chat run back; the Learner finishes behind it, still
holding the team run lock, and the teardown then only releases that lock.
"""
from __future__ import annotations

import asyncio
import queue
import threading
import types as pytypes

import pytest

from aiforge_core.runtime import chat_pipeline as P


@pytest.fixture(autouse=True)
def unlocked():
    """Never leave the process-wide run lock held."""
    yield
    while P._RUN_LOCK.locked():
        try:
            P._RUN_LOCK.release()
        except RuntimeError:  # pragma: no cover - safety net
            break


def _ev(author="aiforge_v6_pipeline", text=None, node=None, route=None):
    parts = [pytypes.SimpleNamespace(text=text, function_call=None,
                                     function_response=None)] if text else []
    return pytypes.SimpleNamespace(
        author=author, partial=False,
        content=pytypes.SimpleNamespace(parts=parts),
        node_info=pytypes.SimpleNamespace(
            path=f"aiforge_v6_pipeline@1/{node}@1" if node else None),
        actions=pytypes.SimpleNamespace(route=route))


# ─── when the answer is final ──────────────────────────────────────────


def test_the_validator_passing_makes_the_answer_final():
    assert P._answer_ready(_ev(node="validator_gate", route="done"))


def test_a_replan_is_not_the_end():
    assert not P._answer_ready(_ev(node="validator_gate", route="replan"))
    assert not P._answer_ready(_ev(node="validator_gate"))


def test_the_learner_speaking_makes_the_answer_final():
    assert P._answer_ready(_ev(author="learner", text="[]"))
    assert P._answer_ready(_ev(node="learner"))


def test_the_doers_work_is_not_the_end():
    assert not P._answer_ready(_ev(author="doer", text="done", node="doer"))
    assert not P._answer_ready(pytypes.SimpleNamespace())


# ─── driving the event stream ──────────────────────────────────────────


def _agen(events):
    async def gen():
        for e in events:
            yield e
    return gen()


def test_the_answer_is_taken_before_the_learner_runs(monkeypatch):
    from aiforge_core.runtime import chat_cancel
    monkeypatch.setattr(chat_cancel, "is_cancelled", lambda sid: False)
    seen: list = []

    async def on_answer(res):
        seen.append(dict(res["by_role"]))

    events = [_ev(author="doer", text="the fix", node="doer"),
              _ev(node="validator_gate", route="done"),
              _ev(author="learner", text='[{"text": "a fact"}]', node="learner")]
    out = asyncio.run(P._drive_run_events(_agen(events), None, queue.Queue(),
                                          None, None, [], on_answer))
    assert seen == [{"doer": "the fix"}], "answered once, without the Learner"
    assert out["answered"] is True
    assert out["by_role"]["learner"], "the Learner still ran to the end"


def test_after_the_answer_a_stop_belongs_to_the_next_turn(monkeypatch):
    """The session's cancel token and steer queue belong to whatever turn the
    user started next; the Learner must neither obey that Stop nor eat its
    steer acknowledgements."""
    from aiforge_core.runtime import chat_cancel, chat_interject
    stop = {"on": False}
    monkeypatch.setattr(chat_cancel, "is_cancelled", lambda sid: stop["on"])
    monkeypatch.setattr(chat_interject, "pop_applied",
                        lambda sid: pytest.fail("took the next turn's steers")
                        if stop["on"] else [])

    async def on_answer(res):
        stop["on"] = True

    events = [_ev(node="validator_gate", route="done"),
              _ev(author="learner", text="facts", node="learner")]
    q: queue.Queue = queue.Queue()
    out = asyncio.run(P._drive_run_events(_agen(events), None, q, 7,
                                          chat_interject, [], on_answer))
    assert out["by_role"]["learner"] == "facts"
    assert not any(e.get("stopped") for e in list(q.queue))


def test_without_a_callback_the_run_is_driven_as_before():
    events = [_ev(node="validator_gate", route="done"),
              _ev(author="learner", text="facts")]
    out = asyncio.run(P._drive_run_events(_agen(events), None, queue.Queue(),
                                          None, None, []))
    assert out["answered"] is False
    assert out["final"] == "facts"


# ─── handing the turn off ──────────────────────────────────────────────


@pytest.fixture
def session_state(monkeypatch):
    from aiforge_core.runtime import (
        chat_approve,
        chat_cancel,
        chat_interject,
        chat_persist,
    )
    seen: dict = {"cleared": [], "persisted": []}
    monkeypatch.setattr(chat_cancel, "is_cancelled", lambda sid: False)
    monkeypatch.setattr(chat_cancel, "finish",
                        lambda sid: seen["cleared"].append("cancel"))
    monkeypatch.setattr(chat_approve, "clear_emitter", lambda sid: None)
    monkeypatch.setattr(chat_approve, "finish",
                        lambda sid: seen["cleared"].append("approve"))
    monkeypatch.setattr(chat_interject, "clear",
                        lambda sid: seen["cleared"].append("steer"))
    monkeypatch.setattr(chat_persist, "persist_turn",
                        lambda **kw: seen["persisted"].append(kw))
    return seen


def test_the_hand_off_closes_the_turn_then_ends_the_tail(session_state):
    q: queue.Queue = queue.Queue()
    P._hand_off_turn(q, 7, "/repo", "build", "the answer", [],
                     [{"slug": "a", "status": "pending"}], 0.0)
    assert q.get()["status"] == "done"
    assert q.get() is P._HANDED_OFF
    assert session_state["persisted"][0]["final_text"] == "the answer"
    assert set(session_state["cleared"]) == {"cancel", "approve", "steer"}


def test_a_handed_off_teardown_only_releases_the_lock(session_state,
                                                      monkeypatch):
    """By now a follow-up turn may own the session: persisting again would
    duplicate the answer, and clearing/finishing by session id would tear down
    the NEW turn's gates and chat run."""
    from aiforge_core.runtime import chat_runs
    monkeypatch.setattr(chat_runs, "finish",
                        lambda sid: pytest.fail("finished the next turn's run"))
    q: queue.Queue = queue.Queue()
    P._RUN_LOCK.acquire()
    P._drive_teardown(None, P._run_lock_gen(), None, 7, "/repo", "build",
                      "the answer", [], [{"slug": "a"}], True, 0.0, q,
                      handed_off=True)
    assert q.get() is P._SENTINEL
    assert q.empty()
    assert session_state["persisted"] == []
    assert session_state["cleared"] == []
    assert not P._RUN_LOCK.locked()


def test_the_tail_ends_on_the_hand_off():
    q: queue.Queue = queue.Queue()
    q.put({"type": "message", "text": "the answer"})
    q.put(P._HANDED_OFF)
    q.put({"type": "thought", "role": "learner", "text": "never read"})
    flags: dict = {}
    assert [e["type"] for e in P._tail_team_queue(q, flags)] == ["message"]
    assert flags == {"saw_real": True, "handed_off": True}


# ─── the producer ends a handed-off run ────────────────────────────────


@pytest.mark.parametrize("path,finished", [
    ({"driver": True, "parallel": False}, False),
    ({"driver": True, "parallel": False, "handed_off": True}, True),
])
def test_the_producer_ends_the_run_only_once_it_is_handed_back(path, finished):
    from aiforge_core.api.routes._chat import _turn_events as TE
    run = pytypes.SimpleNamespace(finished=False)
    run.finish = lambda: setattr(run, "finished", True)
    TE._PRODUCE_SEM.acquire()                 # the slot this turn held
    TE._finalize_produce_turn(
        None, "/repo", "build", "the answer", [], False, True, path, "team",
        0.0, TE._TurnResetContext(None, None, pytypes.SimpleNamespace(),
                                  None, None),
        run, lambda: None)
    assert run.finished is finished


# ─── the real graph, stub models ───────────────────────────────────────


@pytest.fixture
def blocked_learner(monkeypatch):
    """The real team graph on stub models, with the Learner held until the
    test lets it go."""
    from google.adk.models.base_llm import BaseLlm
    from google.adk.models.llm_response import LlmResponse
    from google.genai import types as gt

    import aiforge_core.runtime.pipeline as pl
    monkeypatch.setenv("AIFORGE_ESCALATE_DISABLE", "1")
    monkeypatch.setenv("AIFORGE_OBSERVABILITY_DISABLE", "1")
    monkeypatch.setenv("AIFORGE_CHAT_LEAN", "1")
    replies = {
        "triage": '{"complexity": "moderate", "estimated_files": 3, '
                  '"rationale": "x"}',
        "feedback": "pass\nall good",
        "validator": '{"verdict": "approve", "rationale": "ok", '
                     '"scope_ok": true, "tests_present": true, '
                     '"regression_risk": "low"}',
        "planner": 'PLAN: {"subtickets": '
                   '[{"scope_allowlist_globs": ["src/a/**"]}]}',
        "verifier": '{"verdict": "pass", "rationale": "ok"}',
        "doer": "Here is the answer.",
    }
    st = {"release": threading.Event(), "learner_done": threading.Event()}

    def make(role):
        class _Stub(BaseLlm):
            async def generate_content_async(self, llm_request, stream=False):
                if role == "learner":
                    await asyncio.to_thread(st["release"].wait, 60)
                    st["learner_done"].set()
                yield LlmResponse(content=gt.Content(role="model", parts=[
                    gt.Part(text=replies.get(role, f"{role} output"))]))
        return _Stub(model="stub")

    monkeypatch.setattr(pl, "build_litellm_model", make)
    yield st
    st["release"].set()


def test_the_answer_arrives_while_the_learner_still_runs(blocked_learner,
                                                         tmp_path):
    gen = P.stream_chat_pipeline("add a greeting", cwd=str(tmp_path),
                                 session_id=None, history=[])
    events: list = []
    while True:
        try:
            events.append(next(gen))
        except StopIteration as stop:
            handed_off = stop.value
            break
    assert handed_off is True
    assert not blocked_learner["learner_done"].is_set(), \
        "the turn waited for the Learner"
    msgs = [e for e in events if e.get("type") == "message"]
    assert msgs and msgs[-1]["text"] == "Here is the answer."
    assert events[-1] == {"type": "done"}
    assert P._RUN_LOCK.locked(), "the Learner still owns the team run lock"

    blocked_learner["release"].set()
    assert blocked_learner["learner_done"].wait(60), "the Learner never ran"
    assert P._RUN_LOCK.acquire(timeout=60), "the lock was never released"
    P._RUN_LOCK.release()
