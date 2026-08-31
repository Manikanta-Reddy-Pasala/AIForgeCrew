"""Driving the sequential team pipeline from a chat turn.

A team run mutates the process-global repo root that the sandbox and the git
tools read, so only one may own it at a time — hence a process-wide lock with
an owner GENERATION. The generation is what makes "kill all" safe: it bumps on
a force-release, so the wedged holder's own teardown becomes a no-op instead of
releasing a NEW holder's lock (a plain Lock is unowned) and clobbering its
saved root.

The other rules here are about not lying to the user. The Learner and Refiner
emit JSON verdicts and run AFTER the Doer, so they must never win the final
answer. The Enhancer's ENHANCE_BLOCKED sentinel — its stand-in for the
clarifying question it is not allowed to ask — must stop the run rather than
silently becoming the Planner's brief. And a promoted answer that claims it
applied fixes is checked against the real diff before it is shown.
"""
from __future__ import annotations

import os
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


# ─── mapping ADK events ────────────────────────────────────────────────


def _part(text=None, fc=None, fr=None):
    return pytypes.SimpleNamespace(text=text, function_call=fc,
                                   function_response=fr)


def _event(author="doer", parts=()):
    return pytypes.SimpleNamespace(
        author=author, content=pytypes.SimpleNamespace(parts=list(parts)))


def test_an_agents_text_is_badged_with_which_agent_said_it():
    evs = P.map_event(_event("planner", [_part(text="  the plan  ")]))
    assert evs == [{"type": "thought", "role": "planner", "text": "the plan"}]


def test_a_tool_call_carries_its_name_and_arguments():
    fc = pytypes.SimpleNamespace(name="file_write", args={"path": "a.py"})
    evs = P.map_event(_event("doer", [_part(fc=fc)]))
    assert evs[0]["type"] == "tool" and evs[0]["name"] == "file_write"
    assert evs[0]["args"] == {"path": "a.py"}


def test_a_tool_result_is_summarised():
    fr = pytypes.SimpleNamespace(name="file_read", response={"ok": True})
    assert "file_read →" in P.map_event(_event("doer", [_part(fr=fr)]))[0]["text"]


def test_a_string_result_is_shown_as_it_is():
    fr = pytypes.SimpleNamespace(name="bash", response="done")
    assert P.map_event(_event(parts=[_part(fr=fr)]))[0]["text"] == "bash → done"


def test_an_empty_result_still_names_the_tool():
    fr = pytypes.SimpleNamespace(name="bash", response=None)
    assert P.map_event(_event(parts=[_part(fr=fr)]))[0]["text"] == "bash → "


def test_an_event_with_nothing_in_it_maps_to_nothing():
    assert P.map_event(_event(parts=[_part(text="   ")])) == []
    assert P.map_event(pytypes.SimpleNamespace(author=None, content=None)) == []


def test_the_events_own_text_is_read_for_the_final_answer():
    assert P._event_text(_event(parts=[_part("a"), _part("b")])) == "ab"
    assert P._event_text(pytypes.SimpleNamespace(content=None)) == ""


# ─── the run lock ──────────────────────────────────────────────────────


def test_the_lock_is_released_and_the_root_restored():
    P._RUN_LOCK.acquire()
    gen = P._run_lock_gen()
    os.environ["AIFORGE_REPO_ROOT"] = "/team/run"
    P._release_run_lock(gen, "/before")
    assert os.environ["AIFORGE_REPO_ROOT"] == "/before"
    assert not P._RUN_LOCK.locked()


def test_a_run_that_had_no_root_leaves_none_behind():
    P._RUN_LOCK.acquire()
    os.environ["AIFORGE_REPO_ROOT"] = "/team/run"
    P._release_run_lock(P._run_lock_gen(), None)
    assert "AIFORGE_REPO_ROOT" not in os.environ


def test_a_wedged_holder_cannot_free_the_next_runs_lock(monkeypatch):
    """kill-all bumps the generation; the holder's finally must become a
    no-op or it releases a lock a NEW run now owns."""
    P._RUN_LOCK.acquire()
    stale_gen = P._run_lock_gen()
    assert P.force_release_run_lock() is True
    P._RUN_LOCK.acquire()                       # the next run takes it
    os.environ["AIFORGE_REPO_ROOT"] = "/new/run"
    P._release_run_lock(stale_gen, "/stale")    # the wedged holder tears down
    assert P._RUN_LOCK.locked() is True
    assert os.environ["AIFORGE_REPO_ROOT"] == "/new/run"


def test_force_release_on_an_idle_lock_does_nothing():
    assert P.force_release_run_lock() is False


def test_a_double_release_is_not_an_error():
    gen = P._run_lock_gen()
    P._release_run_lock(gen, None)              # not held
    assert not P._RUN_LOCK.locked()


def test_a_waiting_run_is_told_what_it_is_waiting_for():
    P._RUN_LOCK.acquire()
    q: queue.Queue = queue.Queue()
    got: dict = {}

    def _worker():
        got["gen"] = P._acquire_team_run_lock(None, "/repo", "build", 0.0, q)
    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    first = q.get(timeout=5)
    assert "waiting for another team run" in first["text"]
    P._RUN_LOCK.release()
    t.join(timeout=5)
    assert got["gen"] == P._run_lock_gen()


def test_a_stop_while_waiting_persists_a_stopped_turn(monkeypatch):
    """The api's finally skips persistence for the team path, so without this
    the user message reloads with no assistant turn at all."""
    from aiforge_core.runtime import chat_approve, chat_cancel, chat_persist
    persisted: list = []
    monkeypatch.setattr(chat_cancel, "is_cancelled", lambda sid: True)
    monkeypatch.setattr(chat_cancel, "finish", lambda sid: None)
    monkeypatch.setattr(chat_approve, "clear_emitter", lambda sid: None)
    monkeypatch.setattr(chat_approve, "finish", lambda sid: None)
    monkeypatch.setattr(chat_persist, "persist_turn",
                        lambda **kw: persisted.append(kw))
    q: queue.Queue = queue.Queue()
    assert P._acquire_team_run_lock(7, "/repo", "build it", 0.0, q) is None
    assert persisted[0]["cancelled"] is True and persisted[0]["team"] is True
    assert q.get()["stopped"] is True and q.get() is P._SENTINEL


def test_a_persist_failure_does_not_wedge_the_stop(monkeypatch):
    from aiforge_core.runtime import chat_approve, chat_cancel, chat_persist
    monkeypatch.setattr(chat_cancel, "finish", lambda sid: None)
    monkeypatch.setattr(chat_approve, "clear_emitter", lambda sid: None)
    monkeypatch.setattr(chat_approve, "finish", lambda sid: None)
    monkeypatch.setattr(chat_persist, "persist_turn",
                        lambda **kw: (_ for _ in ()).throw(OSError("db")))
    P._persist_stop_before_start(7, "/repo", "x", 0.0)   # no raise


# ─── conversation continuity ───────────────────────────────────────────


def test_prior_turns_are_replayed_for_a_follow_up():
    """The team pipeline starts a fresh ADK session per message and would
    otherwise be clueless on a follow-up."""
    out = P._history_preamble([{"role": "user", "content": "build a lexer"},
                               {"role": "assistant", "content": "done"},
                               {"role": "user", "content": "and a parser"}])
    assert "User: build a lexer" in out and "Assistant: done" in out
    assert "and a parser" not in out, "the current message is not prior context"


def test_the_preamble_is_bounded():
    history = [{"role": "user", "content": "x" * 2000}] * 30
    out = P._history_preamble(history + [{"role": "user", "content": "now"}])
    assert out.count("User:") == 12 and "x" * 801 not in out


@pytest.mark.parametrize("history", [None, [], [{"role": "user",
                                                 "content": "only me"}]])
def test_a_first_turn_has_no_preamble(history):
    assert P._history_preamble(history) == ""


# ─── the subtask panel ─────────────────────────────────────────────────


def test_a_finished_run_marks_every_subtask_done():
    """The Doer executes the plan in one pass, so without this the panel sits
    at '0/N pending' after the run reports complete."""
    items = [{"slug": "a", "status": "pending"},
             {"slug": "b", "status": "pending"}]
    evs = P._finalize_subtasks(items, run_ok=True, cancelled=False)
    assert [e["status"] for e in evs] == ["done", "done"]
    assert items[0]["status"] == "done", "the persisted dicts agree on reload"


@pytest.mark.parametrize("run_ok,cancelled", [(False, False), (True, True)])
def test_an_error_or_a_stop_marks_them_failed(run_ok, cancelled):
    items = [{"slug": "a", "status": "pending"}]
    P._finalize_subtasks(items, run_ok, cancelled)
    assert items[0]["status"] == "failed"


def test_a_run_with_no_plan_has_no_panel():
    assert P._finalize_subtasks(None, True, False) == []


def test_a_planner_decomposition_becomes_a_live_panel(monkeypatch):
    from aiforge_core.runtime import subtasks_callback
    monkeypatch.setattr(subtasks_callback, "_extract_subtickets",
                        lambda text: [{"slug": "lexer", "goal": "tokenise"},
                                      {"title": "parse it"}])
    ev = P._planner_subtask_event("1. lexer 2. parser")
    assert ev["items"][0] == {"slug": "lexer", "goal": "tokenise",
                              "status": "pending"}
    assert ev["items"][1]["slug"] == "sub-2" and ev["items"][1]["goal"] == "parse it"


def test_a_plan_with_no_subtasks_emits_no_panel(monkeypatch):
    from aiforge_core.runtime import subtasks_callback
    monkeypatch.setattr(subtasks_callback, "_extract_subtickets", lambda t: [])
    assert P._planner_subtask_event("just prose") is None


def test_a_broken_extractor_emits_no_panel(monkeypatch):
    from aiforge_core.runtime import subtasks_callback
    monkeypatch.setattr(subtasks_callback, "_extract_subtickets",
                        lambda t: (_ for _ in ()).throw(RuntimeError("x")))
    assert P._planner_subtask_event("x") is None


# ─── which agent's words become the answer ─────────────────────────────


def test_the_doers_work_is_the_answer():
    """The Learner and Refiner emit JSON verdicts and run last."""
    by_role = {"doer": "added the lexer", "learner": '{"facts": []}'}
    assert P._promote_team_answer(by_role, {}, "Done.", None) == "added the lexer"


def test_a_local_doer_that_emits_no_adk_events_still_wins():
    """The local text_doer writes doer_outcome instead, and the answer used to
    fall through to the Researcher or a bare 'Done.'"""
    assert P._promote_team_answer({"researcher": "here is some context"},
                                  {"doer_outcome": "built it"}, "", None) \
        == "built it"


def test_a_research_only_run_answers_with_the_research():
    assert P._promote_team_answer({"researcher": "the auth flow works like…"},
                                  {}, "", None) == "the auth flow works like…"


def test_a_run_with_nothing_to_say_still_says_something():
    assert P._promote_team_answer({}, {}, "", None) == "Done."


def test_a_blocked_run_asks_for_what_it_needs():
    msg = P._promote_team_answer({"doer": "x"}, {}, "", "no target repo named")
    assert "no target repo named" in msg and msg.startswith("I need more detail")


# ─── the enhancer's stop sentinel ──────────────────────────────────────


def test_the_too_vague_sentinel_stops_the_run():
    """It must never reach the user as a raw thought, and never become the
    Planner's brief."""
    reason = P._enhancer_block_reason(
        {"type": "thought", "role": "enhancer",
         "text": "ENHANCE_BLOCKED: no repo or file was named"})
    assert reason == "no repo or file was named"


def test_a_bare_sentinel_is_still_a_block():
    """With no colon there is nothing to split, so the marker itself becomes
    the reason — the run still stops, which is the part that matters."""
    assert P._enhancer_block_reason({"type": "thought", "role": "enhancer",
                                     "text": "ENHANCE_BLOCKED"}) \
        == "ENHANCE_BLOCKED"


def test_a_sentinel_with_an_empty_reason_falls_back_to_prose():
    assert "too vague" in P._enhancer_block_reason(
        {"type": "thought", "role": "enhancer", "text": "ENHANCE_BLOCKED:  "})


@pytest.mark.parametrize("ev", [
    {"type": "thought", "role": "planner", "text": "ENHANCE_BLOCKED: x"},
    {"type": "tool", "role": "enhancer", "text": "ENHANCE_BLOCKED: x"},
    {"type": "thought", "role": "enhancer", "text": "the spec is clear"},
])
def test_anything_else_is_not_a_block(ev):
    assert P._enhancer_block_reason(ev) is None


# ─── routing one event ─────────────────────────────────────────────────


def _acc():
    return {"emitted_subtasks": False, "sub_items": None}


def test_an_event_reaches_the_client_and_the_transcript():
    q: queue.Queue = queue.Queue()
    steps: list = []
    by_role: dict = {}
    ev = {"type": "thought", "role": "doer", "text": "editing app.py"}
    assert P._process_team_event(ev, q, steps, by_role, _acc()) is None
    assert q.get() is ev and steps == [ev]
    assert by_role["doer"] == "editing app.py"


def test_a_blocked_enhancer_stops_before_anything_is_streamed():
    q: queue.Queue = queue.Queue()
    ev = {"type": "thought", "role": "enhancer", "text": "ENHANCE_BLOCKED: x"}
    assert P._process_team_event(ev, q, [], {}, _acc()) == "x"
    assert q.empty()


def test_the_planners_plan_becomes_a_panel_exactly_once(monkeypatch):
    from aiforge_core.runtime import subtasks_callback
    monkeypatch.setattr(subtasks_callback, "_extract_subtickets",
                        lambda text: [{"slug": "a", "goal": "g"}])
    q: queue.Queue = queue.Queue()
    steps: list = []
    acc = _acc()
    for _ in range(2):
        P._process_team_event({"type": "thought", "role": "planner",
                               "text": "1. a"}, q, steps, {}, acc)
    panels = [s for s in steps if s.get("type") == "subtasks"]
    assert len(panels) == 1
    assert acc["sub_items"] is panels[0]["items"], \
        "the same dicts the teardown reconciles"


def test_a_plain_message_is_streamed_but_not_a_step():
    q: queue.Queue = queue.Queue()
    steps: list = []
    P._process_team_event({"type": "message", "text": "done"}, q, steps, {},
                          _acc())
    assert steps == [] and q.get()["text"] == "done"


# ─── the answer versus the diff ────────────────────────────────────────


@pytest.fixture()
def claim_guard(monkeypatch):
    from aiforge_core.runtime.chat_agent import _context
    state: dict = {"enabled": True, "claims": True}
    monkeypatch.setattr(_context, "_edit_claim_guard_enabled",
                        lambda: state["enabled"])
    monkeypatch.setattr(_context, "_claims_file_edits",
                        lambda msg: state["claims"])
    monkeypatch.setattr(_context, "_edit_claim_disclaimer",
                        lambda msg: "⚠ nothing changed\n" + msg)
    return state


def test_an_applied_fixes_claim_with_an_empty_diff_is_corrected(claim_guard):
    out = P._guard_edit_claim("I applied the fixes", "/repo", "sha0", None, [])
    assert out.startswith("⚠ nothing changed")


def test_a_real_diff_leaves_the_answer_alone(claim_guard):
    msg = "I applied the fixes"
    assert P._guard_edit_claim(msg, "/repo", "sha0", None,
                               [{"type": "changes"}]) == msg


def test_a_non_git_run_gives_no_signal_to_guard_on(claim_guard):
    msg = "I applied the fixes"
    assert P._guard_edit_claim(msg, "/repo", "", None, []) == msg


def test_an_answer_claiming_nothing_is_untouched(claim_guard):
    claim_guard["claims"] = False
    msg = "here is how auth works"
    assert P._guard_edit_claim(msg, "/repo", "sha0", None, []) == msg


def test_the_guard_can_be_switched_off(claim_guard):
    claim_guard["enabled"] = False
    msg = "I applied the fixes"
    assert P._guard_edit_claim(msg, "/repo", "sha0", None, []) == msg


def test_a_broken_guard_never_breaks_the_turn(claim_guard, monkeypatch):
    from aiforge_core.runtime.chat_agent import _context
    monkeypatch.setattr(_context, "_claims_file_edits",
                        lambda msg: (_ for _ in ()).throw(RuntimeError("x")))
    assert P._guard_edit_claim("I fixed it", "/repo", "sha0", None, []) \
        == "I fixed it"


def test_the_changes_diff_is_computed_for_a_git_run(monkeypatch):
    import aiforge_core.runtime.parallel_subtasks as ps
    monkeypatch.setattr(ps, "_emit_changes",
                        lambda cwd, sha, include_worktree=False:
                        iter([{"type": "changes", "files": 2}]))
    assert P._team_change_events("/repo", "sha0", None) == [{"type": "changes",
                                                             "files": 2}]


def test_a_blocked_or_non_git_run_has_no_diff():
    assert P._team_change_events("/repo", "", None) == []
    assert P._team_change_events("/repo", "sha0", "too vague") == []


def test_a_failing_diff_never_breaks_the_turn(monkeypatch):
    import aiforge_core.runtime.parallel_subtasks as ps
    monkeypatch.setattr(ps, "_emit_changes",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("git")))
    assert P._team_change_events("/repo", "sha0", None) == []


# ─── the event queue the client reads ──────────────────────────────────


def test_events_are_tailed_until_the_run_ends():
    q: queue.Queue = queue.Queue()
    q.put({"type": "thought", "text": "working"})
    q.put(P._SENTINEL)
    flags: dict = {}
    assert [e["type"] for e in P._tail_team_queue(q, flags)] == ["thought"]
    assert flags == {"saw_real": True}


def test_a_quiet_run_still_sends_a_heartbeat():
    """A slow local model can leave minute-long gaps, and an idle SSE
    connection gets dropped by the browser or a proxy."""
    q: queue.Queue = queue.Queue()

    class _Q:
        def __init__(self):
            self.n = 0

        def get(self, timeout=None):
            self.n += 1
            if self.n == 1:
                raise queue.Empty
            return P._SENTINEL
    evs = list(P._tail_team_queue(_Q(), {}))
    assert evs == [{"type": "ping"}]


def test_a_stop_is_tracked_as_both_error_and_stopped():
    q: queue.Queue = queue.Queue()
    q.put({"type": "error", "text": "stopped by user", "stopped": True})
    q.put(P._SENTINEL)
    flags: dict = {}
    list(P._tail_team_queue(q, flags))
    assert flags == {"errored": True, "stopped": True}


# ─── steer acknowledgements ────────────────────────────────────────────


def test_a_queued_steer_is_acknowledged_to_the_user(monkeypatch):
    from aiforge_core.runtime import chat_interject, chat_steer
    monkeypatch.setattr(chat_interject, "pop_applied",
                        lambda sid: ["use postgres"])
    monkeypatch.setattr(chat_steer, "applied_event",
                        lambda text: {"type": "steer", "text": text})
    q: queue.Queue = queue.Queue()
    P._emit_steer_acks(7, chat_interject, q)
    assert q.get() == {"type": "steer", "text": "use postgres"}


def test_a_sessionless_run_has_nobody_to_acknowledge(monkeypatch):
    from aiforge_core.runtime import chat_interject
    monkeypatch.setattr(chat_interject, "pop_applied",
                        lambda sid: pytest.fail("looked for steers"))
    P._emit_steer_acks(None, chat_interject, queue.Queue())


# ─── binding the driver thread ─────────────────────────────────────────


def test_the_driver_thread_is_bound_so_stop_can_reach_it(monkeypatch):
    from aiforge_core.runtime import (
        chat_approve,
        chat_cancel,
        chat_interject,
        request_context,
    )
    seen: dict = {}
    monkeypatch.setattr(chat_cancel, "set_active",
                        lambda sid: seen.update(active=sid))
    monkeypatch.setattr(chat_approve, "set_emitter",
                        lambda sid, put: seen.update(emitter=(sid, put)))
    monkeypatch.setattr(chat_interject, "set_steerable",
                        lambda sid, v: seen.update(steerable=(sid, v)))
    monkeypatch.setattr(request_context, "set_session_id",
                        lambda sid: seen.update(ctx=sid))
    q: queue.Queue = queue.Queue()
    P._bind_team_session(7, q)
    assert seen["active"] == 7 and seen["steerable"] == (7, True)
    assert seen["emitter"][0] == 7 and seen["ctx"] == 7
    assert os.environ["AIFORGE_CURRENT_SESSION"] == "7"


def test_a_sessionless_run_only_clears_the_cancel_binding(monkeypatch):
    from aiforge_core.runtime import chat_approve, chat_cancel
    seen: dict = {}
    monkeypatch.setattr(chat_cancel, "set_active",
                        lambda sid: seen.update(active=sid))
    monkeypatch.setattr(chat_approve, "set_emitter",
                        lambda sid, put: pytest.fail("bound an approver"))
    P._bind_team_session(None, queue.Queue())
    assert seen == {"active": None}


# ─── the fallback agent ────────────────────────────────────────────────


@pytest.fixture()
def fallback(monkeypatch):
    from aiforge_core.runtime import (
        chat_approve,
        chat_cancel,
        chat_interject,
        chat_persist,
    )
    import aiforge_core.runtime.chat_agent as ca
    state: dict = {"events": [{"type": "thought", "text": "looking"},
                              {"type": "message", "text": "the answer"},
                              {"type": "done"}],
                   "persisted": [], "finished": [], "started": []}
    monkeypatch.setattr(ca, "run_chat_agent",
                        lambda convo, **kw: iter(state["events"]))
    monkeypatch.setattr(chat_cancel, "start",
                        lambda sid: state["started"].append(sid))
    monkeypatch.setattr(chat_cancel, "is_cancelled", lambda sid: False)
    monkeypatch.setattr(chat_cancel, "finish",
                        lambda sid: state["finished"].append(("cancel", sid)))
    monkeypatch.setattr(chat_approve, "finish",
                        lambda sid: state["finished"].append(("approve", sid)))
    monkeypatch.setattr(chat_interject, "clear",
                        lambda sid: state["finished"].append(("steer", sid)))
    monkeypatch.setattr(chat_persist, "persist_turn",
                        lambda **kw: state["persisted"].append(kw))
    return state


def test_the_fallback_answers_and_persists_itself(fallback):
    evs = list(P._run_pipeline_fallback("build it", "/repo", 7, 0.0))
    assert evs[0]["role"] == "fallback" and "lightweight agent" in evs[0]["text"]
    assert [e["type"] for e in evs[1:]] == ["thought", "message"], \
        "the terminal done belongs to the caller"
    assert fallback["persisted"][0]["final_text"] == "the answer"
    assert fallback["persisted"][0]["mode"] == "team"


def test_stop_is_re_armed_so_the_fallback_can_be_halted(fallback):
    list(P._run_pipeline_fallback("x", "/repo", 7, 0.0))
    assert fallback["started"] == [7]


def test_the_session_state_is_cleared_so_nothing_leaks_next_turn(fallback):
    """A fallback torn down mid-approval would otherwise leave the gates set."""
    list(P._run_pipeline_fallback("x", "/repo", 7, 0.0))
    assert set(fallback["finished"]) == {("cancel", 7), ("approve", 7),
                                         ("steer", 7)}


def test_a_sessionless_fallback_persists_nothing(fallback):
    list(P._run_pipeline_fallback("x", "/repo", None, 0.0))
    assert fallback["persisted"] == []


def test_a_fallback_that_blows_up_just_ends_the_stream(fallback, monkeypatch):
    import aiforge_core.runtime.chat_agent as ca
    monkeypatch.setattr(ca, "run_chat_agent",
                        lambda convo, **kw: (_ for _ in ()).throw(
                            RuntimeError("no model")))
    assert [e["role"] for e in P._run_pipeline_fallback("x", "/repo", 7, 0.0)] \
        == ["fallback"]


# ─── the teardown ──────────────────────────────────────────────────────


def test_a_finished_run_reconciles_persists_and_clears(monkeypatch):
    from aiforge_core.runtime import (
        chat_approve,
        chat_cancel,
        chat_interject,
        chat_persist,
    )
    seen: dict = {"cleared": []}
    monkeypatch.setattr(chat_cancel, "is_cancelled", lambda sid: False)
    monkeypatch.setattr(chat_cancel, "finish",
                        lambda sid: seen["cleared"].append("cancel"))
    monkeypatch.setattr(chat_approve, "clear_emitter", lambda sid: None)
    monkeypatch.setattr(chat_approve, "finish",
                        lambda sid: seen["cleared"].append("approve"))
    monkeypatch.setattr(chat_interject, "clear",
                        lambda sid: seen["cleared"].append("steer"))
    monkeypatch.setattr(chat_persist, "persist_turn",
                        lambda **kw: seen.update(persisted=kw))
    q: queue.Queue = queue.Queue()
    items = [{"slug": "a", "status": "pending"}]
    P._RUN_LOCK.acquire()
    P._drive_teardown(None, P._run_lock_gen(), None, 7, "/repo", "build",
                      "the answer", [], items, True, 0.0, q)
    assert q.get()["status"] == "done" and q.get() is P._SENTINEL
    assert seen["persisted"]["final_text"] == "the answer"
    assert set(seen["cleared"]) == {"cancel", "approve", "steer"}
    assert not P._RUN_LOCK.locked()


def test_a_stopped_run_marks_its_subtasks_failed(monkeypatch):
    from aiforge_core.runtime import (
        chat_approve,
        chat_cancel,
        chat_interject,
        chat_persist,
    )
    monkeypatch.setattr(chat_cancel, "is_cancelled", lambda sid: True)
    monkeypatch.setattr(chat_cancel, "finish", lambda sid: None)
    monkeypatch.setattr(chat_approve, "clear_emitter", lambda sid: None)
    monkeypatch.setattr(chat_approve, "finish", lambda sid: None)
    monkeypatch.setattr(chat_interject, "clear", lambda sid: None)
    persisted: list = []
    monkeypatch.setattr(chat_persist, "persist_turn",
                        lambda **kw: persisted.append(kw))
    q: queue.Queue = queue.Queue()
    P._RUN_LOCK.acquire()
    P._drive_teardown(None, P._run_lock_gen(), None, 7, "/repo", "build", "",
                      [], [{"slug": "a"}], True, 0.0, q)
    assert q.get()["status"] == "failed"
    assert persisted[0]["cancelled"] is True


def test_a_sessionless_run_still_releases_the_lock():
    q: queue.Queue = queue.Queue()
    P._RUN_LOCK.acquire()
    P._drive_teardown(None, P._run_lock_gen(), None, None, "/repo", "x", "",
                      [], None, True, 0.0, q)
    assert q.get() is P._SENTINEL and not P._RUN_LOCK.locked()


def test_the_turn_duration_is_measured():
    import time
    assert P._dur(time.time() - 2) >= 2
    assert P._dur(None) is None
