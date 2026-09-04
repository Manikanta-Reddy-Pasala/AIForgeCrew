"""Team-mode chat: the run lock, event mapping, and which agent's text wins.

Team runs mutate the process-global AIFORGE_REPO_ROOT, so they are serialized
in-process — and the lock has an owner GENERATION because a kill-all can
force-release a wedged holder. The generation is what stops that holder's
finally from releasing a NEW holder's lock and clobbering its env root.

The other decision with history: the Learner and Validator emit JSON verdicts
and run AFTER the Doer, so they must never win the conversational answer. On a
local endpoint the Doer emits no ADK-authored event at all, so its outcome is
read from session state — without that the answer fell through to the
Researcher or a bare "Done."
"""
from __future__ import annotations

import os
import queue
import threading
import types as pytypes

import pytest

from aiforge_core.runtime import chat_pipeline as cp


class _Part:
    def __init__(self, text=None, function_call=None, function_response=None):
        self.text = text
        self.function_call = function_call
        self.function_response = function_response


class _Event:
    def __init__(self, author="agent", parts=()):
        self.author = author
        self.content = pytypes.SimpleNamespace(parts=list(parts))


# ─── the run lock ──────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_lock():
    yield
    while cp._RUN_LOCK.locked():
        try:
            cp._RUN_LOCK.release()
        except RuntimeError:
            break


def test_force_release_frees_a_wedged_run():
    """A team run blocked in an LLM call that outlives a Stop otherwise leaves
    every new chat waiting forever."""
    cp._RUN_LOCK.acquire()
    gen_before = cp._run_lock_gen()
    assert cp.force_release_run_lock() is True
    assert cp._RUN_LOCK.locked() is False
    assert cp._run_lock_gen() == gen_before + 1


def test_force_releasing_an_unheld_lock_is_a_no_op():
    assert cp.force_release_run_lock() is False


def test_the_wedged_holder_does_not_release_the_new_holders_lock(monkeypatch):
    """A plain Lock is unowned, so a stale release would free somebody else's."""
    cp._RUN_LOCK.acquire()
    my_gen = cp._run_lock_gen()
    cp.force_release_run_lock()                  # kill-all: gen bumped
    cp._RUN_LOCK.acquire()                       # a NEW run takes the lock
    monkeypatch.setenv("AIFORGE_REPO_ROOT", "/new-run-root")
    cp._release_run_lock(my_gen, "/old-root")    # the wedged holder's finally
    assert cp._RUN_LOCK.locked() is True
    assert os.environ["AIFORGE_REPO_ROOT"] == "/new-run-root"


def test_the_owning_holder_restores_the_env_and_releases(monkeypatch):
    cp._RUN_LOCK.acquire()
    monkeypatch.setenv("AIFORGE_REPO_ROOT", "/run-root")
    cp._release_run_lock(cp._run_lock_gen(), "/previous")
    assert cp._RUN_LOCK.locked() is False
    assert os.environ["AIFORGE_REPO_ROOT"] == "/previous"


def test_a_run_with_no_previous_root_clears_the_variable(monkeypatch):
    cp._RUN_LOCK.acquire()
    monkeypatch.setenv("AIFORGE_REPO_ROOT", "/run-root")
    cp._release_run_lock(cp._run_lock_gen(), None)
    assert "AIFORGE_REPO_ROOT" not in os.environ


def test_releasing_an_already_free_lock_is_not_an_error():
    cp._release_run_lock(cp._run_lock_gen(), None)


# ─── mapping ADK events ────────────────────────────────────────────────


def test_agent_text_becomes_a_role_badged_thought():
    out = cp.map_event(_Event("planner", [_Part(text="  the plan  ")]))
    assert out == [{"type": "thought", "role": "planner", "text": "the plan"}]


def test_a_tool_call_becomes_a_tool_event():
    fc = pytypes.SimpleNamespace(name="file_write", args={"path": "a.py"})
    out = cp.map_event(_Event("doer", [_Part(function_call=fc)]))
    assert out == [{"type": "tool", "role": "doer", "name": "file_write",
                    "args": {"path": "a.py"}, "result": {"by": "doer"}}]


def test_a_tool_result_is_summarised():
    fr = pytypes.SimpleNamespace(name="file_read", response={"content": "x" * 500})
    out = cp.map_event(_Event("doer", [_Part(function_response=fr)]))
    assert out[0]["text"].startswith("file_read → ")
    assert len(out[0]["text"]) < 260


def test_a_string_tool_result_is_kept_whole():
    fr = pytypes.SimpleNamespace(name="grep", response="two hits")
    assert cp.map_event(_Event("doer", [_Part(function_response=fr)]))[0]["text"] \
        == "grep → two hits"


def test_an_empty_tool_result():
    fr = pytypes.SimpleNamespace(name="noop", response=None)
    assert cp.map_event(_Event("doer", [_Part(function_response=fr)]))[0]["text"] \
        == "noop → "


def test_blank_text_produces_no_event():
    assert cp.map_event(_Event("planner", [_Part(text="   ")])) == []


def test_an_event_with_no_content_maps_to_nothing():
    assert cp.map_event(pytypes.SimpleNamespace(author="x", content=None)) == []


def test_the_final_text_is_the_concatenated_parts():
    assert cp._event_text(_Event("doer", [_Part(text="a"), _Part(text="b")])) == "ab"


# ─── history continuity ────────────────────────────────────────────────


def test_prior_turns_are_rendered_for_the_fresh_adk_session():
    """The team pipeline starts a new session per message and would otherwise
    be clueless on a follow-up."""
    out = cp._history_preamble([{"role": "user", "content": "add a cache"},
                                {"role": "assistant", "content": "done"},
                                {"role": "user", "content": "now test it"}])
    assert "CONVERSATION SO FAR" in out
    assert "User: add a cache" in out
    assert "Assistant: done" in out
    assert "now test it" not in out          # the current message is dropped


def test_no_history_is_no_preamble():
    assert cp._history_preamble(None) == ""
    assert cp._history_preamble([]) == ""


def test_a_first_turn_has_nothing_prior():
    assert cp._history_preamble([{"role": "user", "content": "hi"}]) == ""


def test_only_the_last_dozen_turns_are_rendered():
    hist = [{"role": "user", "content": f"m{i}"} for i in range(30)]
    assert cp._history_preamble(hist).count("User:") == 12


def test_a_long_turn_is_truncated():
    hist = [{"role": "user", "content": "x" * 2000}, {"role": "user", "content": "now"}]
    assert len(cp._history_preamble(hist)) < 1000


# ─── the subtask panel ─────────────────────────────────────────────────


def test_a_clean_finish_marks_every_subtask_done():
    """The Doer executes the plan in one pass, so without this the panel sits
    at "0/N pending" after the run reports complete."""
    items = [{"slug": "a", "status": "pending"}, {"slug": "b", "status": "pending"}]
    events = cp._finalize_subtasks(items, run_ok=True, cancelled=False)
    assert [i["status"] for i in items] == ["done", "done"]
    assert events[0] == {"type": "subtask_update", "slug": "a", "status": "done"}


@pytest.mark.parametrize("ok,cancelled", [(False, False), (True, True)])
def test_a_failed_or_stopped_run_marks_them_failed(ok, cancelled):
    items = [{"slug": "a", "status": "pending"}]
    cp._finalize_subtasks(items, run_ok=ok, cancelled=cancelled)
    assert items[0]["status"] == "failed"


def test_no_subtasks_produces_no_events():
    assert cp._finalize_subtasks(None, True, False) == []


def test_a_planner_decomposition_becomes_a_live_panel(monkeypatch):
    import aiforge_core.runtime.subtasks_callback as sc
    monkeypatch.setattr(sc, "_extract_subtickets",
                        lambda text: [{"slug": "store", "goal": "the store"},
                                      {"title": "the cli"}])
    ev = cp._planner_subtask_event("1. store — the store")
    assert ev["items"][0] == {"slug": "store", "goal": "the store",
                              "status": "pending"}
    assert ev["items"][1]["slug"] == "sub-2"
    assert ev["items"][1]["goal"] == "the cli"


def test_a_plan_with_no_subtasks_shows_no_panel(monkeypatch):
    import aiforge_core.runtime.subtasks_callback as sc
    monkeypatch.setattr(sc, "_extract_subtickets", lambda text: [])
    assert cp._planner_subtask_event("prose only") is None


def test_an_unparseable_plan_shows_no_panel(monkeypatch):
    import aiforge_core.runtime.subtasks_callback as sc
    monkeypatch.setattr(sc, "_extract_subtickets",
                        lambda text: (_ for _ in ()).throw(ValueError("bad")))
    assert cp._planner_subtask_event("x") is None


# ─── which answer the user sees ────────────────────────────────────────


def test_the_doers_work_wins_over_the_learners_verdict():
    """The Learner runs last and emits facts JSON, not prose."""
    by_role = {"doer": "I added the cache", "learner": '[{"text": "fact"}]'}
    assert cp._promote_team_answer(by_role, {}, "final text", None) == \
        "I added the cache"


def test_a_local_doer_answer_is_read_from_state():
    """The local text_doer emits no ADK "doer"-authored event, so the answer
    used to fall through to the Researcher or a bare "Done."."""
    assert cp._promote_team_answer({"researcher": "found things"},
                                   {"doer_outcome": "I added the cache"},
                                   "", None) == "I added the cache"


def test_the_researcher_is_the_next_fallback():
    assert cp._promote_team_answer({"researcher": "found things"}, {}, "", None) \
        == "found things"


def test_with_nothing_at_all_the_turn_still_answers():
    assert cp._promote_team_answer({}, {}, "", None) == "Done."


def test_an_enhancer_block_asks_for_detail_instead():
    out = cp._promote_team_answer({"doer": "x"}, {}, "y", "no target named")
    assert "need more detail" in out
    assert "no target named" in out


# ─── the enhancer sentinel ─────────────────────────────────────────────


def test_the_sentinel_is_recognised_and_carries_its_reason():
    ev = {"type": "thought", "role": "enhancer",
          "text": "ENHANCE_BLOCKED: no file or module named"}
    assert cp._enhancer_block_reason(ev) == "no file or module named"


def test_an_empty_reason_falls_back_to_a_default():
    ev = {"type": "thought", "role": "enhancer", "text": "ENHANCE_BLOCKED:"}
    assert "too vague" in cp._enhancer_block_reason(ev)


def test_a_colon_less_sentinel_reports_the_marker_itself():
    """Known shape: with no colon the split returns the whole line, so the
    reason reads "ENHANCE_BLOCKED" rather than the default sentence. It still
    stops the run, which is what matters."""
    ev = {"type": "thought", "role": "enhancer", "text": "ENHANCE_BLOCKED"}
    assert cp._enhancer_block_reason(ev) == "ENHANCE_BLOCKED"


@pytest.mark.parametrize("ev", [
    {"type": "thought", "role": "planner", "text": "ENHANCE_BLOCKED: x"},
    {"type": "tool", "role": "enhancer", "text": "ENHANCE_BLOCKED: x"},
    {"type": "thought", "role": "enhancer", "text": "a normal thought"},
])
def test_other_events_are_not_the_sentinel(ev):
    assert cp._enhancer_block_reason(ev) is None


# ─── routing one event ─────────────────────────────────────────────────


@pytest.fixture
def routing():
    return {"q": queue.Queue(), "steps": [], "by_role": {},
            "acc": {"emitted_subtasks": False, "sub_items": None}}


def test_a_thought_is_queued_recorded_and_tracked_per_role(routing):
    ev = {"type": "thought", "role": "doer", "text": "I added the cache"}
    assert cp._process_team_event(ev, routing["q"], routing["steps"],
                                  routing["by_role"], routing["acc"]) is None
    assert routing["q"].get_nowait() == ev
    assert routing["steps"] == [ev]
    assert routing["by_role"]["doer"] == "I added the cache"


def test_a_message_event_is_queued_but_not_recorded_as_a_step(routing):
    ev = {"type": "message", "text": "done"}
    cp._process_team_event(ev, routing["q"], routing["steps"], routing["by_role"],
                           routing["acc"])
    assert routing["q"].get_nowait() == ev
    assert routing["steps"] == []


def test_the_sentinel_stops_the_run_before_it_is_shown(routing):
    """It must never reach the user as a raw thought, and must stop the run —
    otherwise it silently becomes the Planner/Doer's brief."""
    ev = {"type": "thought", "role": "enhancer", "text": "ENHANCE_BLOCKED: vague"}
    assert cp._process_team_event(ev, routing["q"], routing["steps"],
                                  routing["by_role"], routing["acc"]) == "vague"
    assert routing["q"].empty()
    assert routing["steps"] == []


def test_the_planners_panel_is_emitted_once(routing, monkeypatch):
    monkeypatch.setattr(cp, "_planner_subtask_event",
                        lambda text: {"type": "subtasks",
                                      "items": [{"slug": "a"}]})
    ev = {"type": "thought", "role": "planner", "text": "1. a"}
    cp._process_team_event(ev, routing["q"], routing["steps"], routing["by_role"],
                           routing["acc"])
    cp._process_team_event(dict(ev), routing["q"], routing["steps"],
                           routing["by_role"], routing["acc"])
    kinds = []
    while not routing["q"].empty():
        kinds.append(routing["q"].get_nowait()["type"])
    assert kinds.count("subtasks") == 1
    assert routing["acc"]["sub_items"] == [{"slug": "a"}]


# ─── change events + the edit-claim guard ──────────────────────────────


def test_the_working_tree_diff_is_included(monkeypatch):
    """The sequential Doer edits the tree in place, so include_worktree is on."""
    import aiforge_core.runtime.parallel_subtasks as ps
    seen: dict = {}

    def _emit(cwd, sha, include_worktree=False):
        seen.update(cwd=cwd, sha=sha, worktree=include_worktree)
        return iter([{"type": "changes", "files": []}])
    monkeypatch.setattr(ps, "_emit_changes", _emit)
    out = cp._team_change_events("/repo", "abc123", None)
    assert out == [{"type": "changes", "files": []}]
    assert seen["worktree"] is True


def test_a_non_git_run_has_no_diff():
    assert cp._team_change_events("/repo", "", None) == []


def test_an_enhancer_blocked_turn_has_no_diff():
    assert cp._team_change_events("/repo", "abc", "too vague") == []


def test_a_failing_diff_never_breaks_the_turn(monkeypatch):
    import aiforge_core.runtime.parallel_subtasks as ps
    monkeypatch.setattr(ps, "_emit_changes",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no git")))
    assert cp._team_change_events("/repo", "abc", None) == []


@pytest.fixture
def claim_guard(monkeypatch):
    import aiforge_core.runtime.chat_agent._context as ctx
    monkeypatch.setattr(ctx, "_edit_claim_guard_enabled", lambda: True)
    monkeypatch.setattr(ctx, "_claims_file_edits", lambda msg: "applied" in msg)
    monkeypatch.setattr(ctx, "_edit_claim_disclaimer",
                        lambda msg: "NOTE: nothing changed.\n" + msg)


def test_an_edit_claim_with_an_empty_diff_is_flagged(claim_guard):
    out = cp._guard_edit_claim("I applied the fix", "/repo", "abc", None, [])
    assert out.startswith("NOTE: nothing changed.")


def test_a_claim_backed_by_a_real_diff_is_untouched(claim_guard):
    msg = "I applied the fix"
    assert cp._guard_edit_claim(msg, "/repo", "abc", None,
                                [{"type": "changes"}]) == msg


def test_an_answer_that_claims_nothing_is_untouched(claim_guard):
    msg = "Here is what I found"
    assert cp._guard_edit_claim(msg, "/repo", "abc", None, []) == msg


def test_a_non_git_run_gives_no_signal_to_guard_on(claim_guard):
    msg = "I applied the fix"
    assert cp._guard_edit_claim(msg, "/repo", "", None, []) == msg


def test_a_broken_guard_never_breaks_a_turn(monkeypatch):
    import aiforge_core.runtime.chat_agent._context as ctx
    monkeypatch.setattr(ctx, "_edit_claim_guard_enabled",
                        lambda: (_ for _ in ()).throw(RuntimeError("bad")))
    msg = "I applied the fix"
    assert cp._guard_edit_claim(msg, "/repo", "abc", None, []) == msg


# ─── steer acks + session binding ──────────────────────────────────────


def test_a_queued_steer_gets_an_ack(monkeypatch):
    from aiforge_core.runtime import chat_interject
    monkeypatch.setattr(chat_interject, "pop_applied", lambda sid: ["use an LRU"])
    q = queue.Queue()
    cp._emit_steer_acks(7, chat_interject, q)
    ev = q.get_nowait()
    assert ev["type"] == "thought"
    assert "use an LRU" in ev["text"]


def test_a_ticketless_run_needs_no_acks(monkeypatch):
    from aiforge_core.runtime import chat_interject
    monkeypatch.setattr(chat_interject, "pop_applied",
                        lambda sid: pytest.fail("polled with no session"))
    cp._emit_steer_acks(None, chat_interject, queue.Queue())


def test_the_driver_thread_is_bound_to_the_session(monkeypatch):
    from aiforge_core.runtime import (chat_approve, chat_cancel, chat_interject,
                                      request_context)
    seen: dict = {}
    monkeypatch.setattr(chat_cancel, "set_active", lambda sid: seen.update(active=sid))
    monkeypatch.setattr(chat_approve, "set_emitter",
                        lambda sid, put: seen.update(emitter=sid))
    monkeypatch.setattr(chat_interject, "set_steerable",
                        lambda sid, on: seen.update(steerable=(sid, on)))
    monkeypatch.setattr(request_context, "set_session_id",
                        lambda sid: seen.update(ctx=sid))
    cp._bind_team_session(7, queue.Queue())
    assert seen == {"active": 7, "emitter": 7, "steerable": (7, True), "ctx": 7}
    assert os.environ["AIFORGE_CURRENT_SESSION"] == "7"
    os.environ.pop("AIFORGE_CURRENT_SESSION", None)


def test_a_sessionless_run_only_clears_the_active_marker(monkeypatch):
    from aiforge_core.runtime import chat_approve, chat_cancel
    seen: dict = {}
    monkeypatch.setattr(chat_cancel, "set_active", lambda sid: seen.update(active=sid))
    monkeypatch.setattr(chat_approve, "set_emitter",
                        lambda sid, put: pytest.fail("bound a sessionless run"))
    cp._bind_team_session(None, queue.Queue())
    assert seen == {"active": None}


# ─── turn duration ─────────────────────────────────────────────────────


def test_a_turn_reports_its_wall_clock(monkeypatch):
    monkeypatch.setattr(cp.time, "time", lambda: 1000.5)
    assert cp._dur(1000.0) == 0.5


def test_a_turn_that_never_started_has_no_duration():
    assert cp._dur(None) is None
