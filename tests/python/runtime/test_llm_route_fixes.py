"""The waits around a model outage, seam by seam: one layer owns the bound,
chats do not hold generation slots while they wait, a parallel run's cancel
reaches its subtasks, abandoned side calls do not wait, an LLM issue stops
the turn, the overlap gate is the routing gate."""
from __future__ import annotations

import threading
import time

import pytest

from aiforge_core.llm import endpoint_breaker, model_outage, model_wait


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    def _gaps(cap=None):
        while True:
            yield 0.02
    monkeypatch.setattr(model_wait, "delays", _gaps)
    model_wait._reset_for_tests()
    endpoint_breaker.reset()
    yield
    model_wait._reset_for_tests()


def _drive(gen):
    events = []
    try:
        while True:
            events.append(next(gen))
    except StopIteration as stop:
        return events, stop.value


# ── 2: one layer owns the wait; the configured bound is the bound ───────────

def test_a_bounded_client_wait_is_not_waited_again_by_the_chat(monkeypatch):
    from aiforge_core.llm import client
    from aiforge_core.runtime.chat_agent._turn import _completion
    monkeypatch.setenv("AIFORGE_LLM_WAIT_MAX_S", "0.1")
    monkeypatch.setattr(model_wait, "probe", lambda *a, **k: False)
    calls = []

    def _impl(*a, **k):
        calls.append(1)
        raise ConnectionRefusedError("refused")
    monkeypatch.setattr(client, "_complete_impl", _impl)
    st = type("S", (), {"convo": [], "edits_made": 0})()
    t0 = time.monotonic()
    evs, out = _drive(_completion._run_completion(
        st, "chat", lambda role, convo: client.complete(role, convo), None, None))
    assert out is _completion._RETRY_STOP
    assert calls == [1]                          # no sweep, no second wait
    assert time.monotonic() - t0 < 2.0
    assert any(e.get("type") == "stopped" for e in evs)


def test_a_completion_fn_that_does_not_wait_is_still_waited_for(monkeypatch):
    """A complete_fn outside the client (no inner wait): the chat waits."""
    from aiforge_core.runtime.chat_agent._turn import _completion
    monkeypatch.setenv("AIFORGE_LLM_WAIT_MAX_S", "0")
    monkeypatch.setattr(_completion, "_complete_cancellable",
                        lambda fn, role, convo, sid: fn(role, convo))
    monkeypatch.setattr("aiforge_core.runtime.run_interrupt.pause",
                        lambda *a, **k: None)
    left = [5]

    def fn(role, convo):
        if left[0]:
            left[0] -= 1
            raise ConnectionRefusedError("refused")
        return "FINAL: back"
    evs, out = _drive(_completion._retry_completion(
        fn, "chat", [], None, ConnectionRefusedError("refused"), None, None,
        None, wait_s=0))
    assert out == "FINAL: back"


# ── 1: an LLM issue stops the turn and asks the user ────────────────────────

def test_an_llm_issue_stops_the_chat_and_awaits_the_user():
    from aiforge_core.runtime.chat_agent._turn import _completion
    issue = model_outage.LLMRequestFailing("http://m/v1", 4,
                                           TimeoutError("504 gateway"))
    called = []
    evs, out = _drive(_completion._retry_completion(
        lambda *a: called.append(1), "chat", [], None, issue, None, None, None,
        wait_s=0))
    assert out is _completion._RETRY_STOP
    assert called == []                                      # never re-sent
    msg = [e for e in evs if e.get("type") == "message"]
    assert msg and msg[0].get("awaiting_input") and "LLM issue" in msg[0]["text"]
    assert {"type": "stopped", "reason": "llm_request_fails"}.items() <= [
        e for e in evs if e.get("type") == "stopped"][0].items()


def test_a_ticket_attempt_fails_with_the_llm_issue_reason():
    from aiforge_core.runtime.adk_runner import _orchestrate, _pipeline
    issue = model_outage.LLMRequestFailing("http://m/v1", 4, TimeoutError("x"))
    wrapped = RuntimeError("pipeline")
    wrapped.__cause__ = issue
    assert _pipeline._abort_name(wrapped) == "llm_request_fails"
    assert _orchestrate._issue_meta(wrapped)["failure_reason"] == \
        "llm_request_fails"
    assert _orchestrate._issue_meta(ValueError("x")) == {}


def test_the_text_doer_turns_the_llm_issue_into_a_failed_attempt(monkeypatch,
                                                                  tmp_path):
    from aiforge_core.runtime import text_doer

    def _pass(seed, *, out, **kw):
        raise model_outage.LLMRequestFailing("", 0, None, "LLM issue: x")
    monkeypatch.setattr(text_doer, "_one_pass", _pass)
    monkeypatch.setattr(text_doer, "_build_seed", lambda state: "seed")
    res = text_doer.run_text_doer({"ticket_title": "t"}, str(tmp_path))
    assert res["llm_issue"] == "LLM issue: x"


# ── 3: no generation slot is held while waiting for a down model ────────────

def _consume(gen, box):
    try:
        while True:
            box.setdefault("evs", []).append(next(gen))
    except StopIteration as stop:
        box["out"] = stop.value
    except Exception as exc:  # noqa: BLE001
        box["err"] = exc


def test_a_chat_waiting_for_its_model_frees_its_slot(monkeypatch):
    from aiforge_core.runtime import chat_cancel
    from aiforge_core.runtime.chat_agent._context import _generation as g
    monkeypatch.setenv("AIFORGE_LLM_WAIT_MAX_S", "0")
    monkeypatch.setattr(g, "_GEN_SEM", threading.BoundedSemaphore(1))
    up = threading.Event()
    probing = threading.Event()

    def _probe(*a, **k):
        probing.set()
        return up.is_set()
    monkeypatch.setattr(model_wait, "probe", _probe)
    first = [True]

    def down_fn(role, convo):
        def call():
            if first[0]:
                first[0] = False
                raise ConnectionRefusedError("refused")
            return "FINAL: a"
        return model_wait.call_with_wait(call, url="http://down/v1")

    for sid in (5101, 5102):
        chat_cancel.start(sid)
    try:
        a: dict = {}
        ta = threading.Thread(target=_consume, args=(
            g._complete_live(down_fn, "chat", [], 5101, stream=False), a))
        ta.start()
        assert probing.wait(5)
        b: dict = {}
        tb = threading.Thread(target=_consume, args=(
            g._complete_live(lambda r, c: "FINAL: b", "chat", [], 5102,
                             stream=False), b))
        tb.start()
        tb.join(3)
        assert b.get("out") == "FINAL: b"      # not blocked by A's wait
        up.set()
        ta.join(5)
        assert a.get("out") == "FINAL: a"
        assert g._GEN_SEM.acquire(timeout=1)   # every slot came back
        g._GEN_SEM.release()
    finally:
        for sid in (5101, 5102):
            chat_cancel.finish(sid)


def test_a_chat_blocked_on_a_busy_slot_says_it_is_queued(monkeypatch):
    from aiforge_core.runtime import chat_cancel
    from aiforge_core.runtime.chat_agent._context import _generation as g
    sem = threading.BoundedSemaphore(1)
    monkeypatch.setattr(g, "_GEN_SEM", sem)
    sem.acquire()
    threading.Timer(0.9, sem.release).start()
    chat_cancel.start(5103)
    try:
        evs, out = _drive(g._complete_live(lambda r, c: "FINAL: ok", "chat",
                                           [], 5103, stream=False))
    finally:
        chat_cancel.finish(5103)
    assert out == "FINAL: ok"
    assert sum("queued" in e.get("text", "") for e in evs) == 1


# ── 4 + 5: the claim's loss reaches the subtasks; a takeover is a loss ──────

def test_subtask_threads_inherit_the_callers_scope(monkeypatch):
    from aiforge_core.runtime import cmd_jobs
    from aiforge_core.runtime.parallel_subtasks import _orchestrate as o
    seen = []

    def _sub(repo_root, base_branch, ticket_id, s, *rest):
        seen.append((model_wait.cancel_reason(), cmd_jobs._OWNER.get()))
        return {"slug": s["slug"], "ok": True}
    monkeypatch.setattr(o, "_run_subtask", _sub)
    lost = threading.Event()
    lost.set()
    tok = cmd_jobs.set_owner("ticket-9")
    try:
        with model_wait.scope(lost, "ticket claim lost"):
            o._dispatch_batch([{"slug": "a"}, {"slug": "b"}], repo_root=".",
                              base_branch="main", ticket_id=9, run_one=None,
                              validate_one=None, on_status=None, run_token="t",
                              should_cancel=None)
    finally:
        cmd_jobs.reset_owner(tok)
    assert seen and all(r == ("ticket claim lost", "ticket-9") for r in seen)


def test_hold_claim_yields_its_lost_event_as_the_cancel(monkeypatch):
    from aiforge_core.runtime.parallel_subtasks._runners import _claim_cancel
    from aiforge_core.tickets import lease, store
    monkeypatch.setattr(store, "renew_claim", lambda tid, *a: False)
    monkeypatch.setattr(lease, "_claim_token", lambda tid: None)
    monkeypatch.setattr(lease, "_stop_jobs", lambda owner: None)
    with lease.hold_claim(7, interval_s=0.05) as lost:
        cancel = _claim_cancel(lost)
        assert lost.wait(2)
        assert cancel() is True


# ── 6: an abandoned side call never waits for the model ─────────────────────

def test_the_rule_capture_classify_does_not_wait(monkeypatch, tmp_path):
    from aiforge_core.api.routes._chat import _routing
    seen = []

    class _RC:
        @staticmethod
        def classify(prompt, repo=None, session_id=None):
            seen.append(model_wait._OPTIONAL.get())
            return {"category": "none"}
    assert _routing._run_capture_pass(_RC, "always use tabs", "r",
                                      str(tmp_path), None) is None
    assert seen == [True]


def test_the_note_curator_does_not_wait(monkeypatch, tmp_path):
    from aiforge_core.api.routes._chat import _producer
    from aiforge_core.runtime import note_curator
    seen = []
    monkeypatch.setattr(note_curator, "stale_note_path", lambda cwd: "n.md")
    monkeypatch.setattr(note_curator, "curate_note",
                        lambda p: seen.append(model_wait._OPTIONAL.get()) or {})
    list(_producer._note_staleness_notice(str(tmp_path)))
    assert seen == [True]


# ── 8: the overlap gate IS the routing gate ─────────────────────────────────

def test_the_overlap_gate_uses_the_routing_predicate(monkeypatch):
    from aiforge_core.api.routes._chat import _overlap, _routing
    from aiforge_core.runtime import chat_router, turn_router
    monkeypatch.setattr(turn_router, "is_followup", lambda h: False)
    for verdict in (True, False):
        monkeypatch.setattr(_routing, "_classify_needed",
                            lambda cr, p, v=verdict: v)
        assert _overlap._classifier_will_run(
            "make a thing", [], team=False, quick=False,
            single_agent=False) is verdict
    monkeypatch.undo()
    short_build = [p for p in ("build a todo app", "create a login page",
                               "implement the api", "add a rest endpoint")
                   if chat_router.is_short_prompt(p)
                   and _routing._classify_needed(chat_router, p)]
    for p in short_build:          # the case the old copy got wrong
        assert _overlap._classifier_will_run(
            p, [], team=False, quick=False, single_agent=False)
