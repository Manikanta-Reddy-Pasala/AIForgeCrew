import json

import pytest

from aiforge_core.runtime import chat_agent as ca


def _scripted(outputs):
    """Return a complete_fn that yields the given outputs in order."""
    seq = list(outputs)

    def _fn(role, messages, **kw):
        return seq.pop(0)
    return _fn


def _collect(gen):
    return list(gen)


def test_plan_mode_allows_jira_confluence_reads(tmp_path):
    """Plan-mode gate regression: the jira/confluence READ suite + the
    context_gather dossier are read-only and must NOT be blocked — the gate
    list drifted when those tools shipped, so plan mode 'couldn't read jira'
    while simple chat could."""
    for tool in ("context_gather", "jira_read", "confluence_read",
                 "jira_worklog", "confluence_descendants", "jira_boards"):
        fn = _scripted([
            f'ACTION: {tool}\nARGS_JSON: {{"key": "CLR-1", "id": "1"}}',
            "FINAL: done",
        ])
        evs = _collect(ca.run_chat_agent(
            [{"role": "user", "content": "read CLR-1"}], cwd=str(tmp_path),
            complete_fn=fn, mode="plan"))
        tools = [e for e in evs if e["type"] == "tool" and e["name"] == tool]
        assert tools, tool
        # may fail as not-configured — but NEVER as a plan_mode block
        assert tools[0]["result"].get("blocked") != "plan_mode", tool


def test_split_asks_variants():
    # multi-sentence with connector + question → parts detected
    asks = ca._split_asks(
        "fix the login timeout bug. also why does the meter reset on new "
        "messages? and add a retry to the sync client")
    assert len(asks) == 3
    # bullets count as-is
    asks = ca._split_asks("please handle:\n- update the README\n- add tests\n"
                          "- push the changes")
    assert len(asks) == 3
    # single ask → no checklist
    assert ca._split_asks("fix the login timeout bug in auth.py") == []
    assert ca._split_asks("thanks") == []


def test_multiask_final_gate_forces_completeness_pass(tmp_path):
    """FINAL on a multi-part message triggers ONE self-check turn with the
    checklist; the second FINAL passes through."""
    prompts: list[str] = []

    def fn(role, convo):
        prompts.append(convo[-1]["content"] if isinstance(
            convo[-1]["content"], str) else "")
        if len(prompts) == 1:
            return "FINAL: 1) fixed the bug"
        return "FINAL: 1) fixed the bug 2) meter resets because X 3) retry added"

    evs = _collect(ca.run_chat_agent(
        [{"role": "user", "content":
          "fix the login bug. also why does the meter reset? and add a retry "
          "to the sync client"}],
        cwd=str(tmp_path), complete_fn=fn))
    final = [e for e in evs if e["type"] == "message"][-1]
    assert "retry added" in final["text"]          # second (complete) FINAL won
    assert any("completeness check" in p for p in prompts)
    # checklist also pinned in the system prompt
    # (fn saw convo; check via the first call's system message)


def test_multiask_tracked_as_subtasks(tmp_path):
    """Multi-part turn: checklist surfaces in the UI subtasks dock, the agent
    flips items via plan_progress, and FINAL closes out any stragglers."""
    fn = _scripted([
        'ACTION: plan_progress\nARGS_JSON: {"slug": "part-1", "status": "done"}',
        "FINAL: 1) bug fixed 2) meter explained 3) retry added",
        "FINAL: 1) bug fixed 2) meter explained 3) retry added",
    ])
    evs = _collect(ca.run_chat_agent(
        [{"role": "user", "content":
          "fix the login bug. also why does the meter reset? and add a retry "
          "to the sync client"}],
        cwd=str(tmp_path), complete_fn=fn))
    docks = [e for e in evs if e["type"] == "subtasks"]
    assert docks and len(docks[0]["items"]) == 3
    assert docks[0]["items"][0]["slug"] == "part-1"
    ups = [(e["slug"], e["status"]) for e in evs if e["type"] == "subtask_update"]
    assert ("part-1", "done") in ups          # agent flipped it via the tool
    # FINAL closes out the rest so nothing lingers pending
    assert ("part-2", "done") in ups and ("part-3", "done") in ups


def test_multiask_single_question_untouched(tmp_path):
    calls = {"n": 0}

    def fn(role, convo):
        calls["n"] += 1
        return "FINAL: done"

    evs = _collect(ca.run_chat_agent(
        [{"role": "user", "content": "fix the login timeout bug in auth.py"}],
        cwd=str(tmp_path), complete_fn=fn))
    assert calls["n"] == 1                          # no extra self-check call
    assert [e for e in evs if e["type"] == "message"][-1]["text"] == "done"


def test_cancellable_complete_returns_sentinel_when_cancelled():
    """H1: a cancel set while the LLM call runs makes the wrapper return the
    _CANCELLED sentinel promptly (the call is abandoned, not awaited)."""
    import threading
    import time as _t
    from aiforge_core.runtime import chat_cancel
    sid = 77123
    chat_cancel.start(sid)

    def slow(role, messages, **kw):
        _t.sleep(5)          # simulate a slow generation
        return "FINAL: too late"

    box = {}
    def run():
        box["out"] = ca._complete_cancellable(slow, "doer", [], sid)
    th = threading.Thread(target=run, daemon=True)
    th.start()
    _t.sleep(0.3)
    chat_cancel.cancel(sid)  # Stop pressed mid-generation
    th.join(timeout=2)
    assert not th.is_alive(), "wrapper did not return promptly on cancel"
    assert box["out"] is ca._CANCELLED
    chat_cancel.finish(sid)


def test_cancellable_complete_passes_through_empty():
    """A legitimately-empty completion is returned as-is (not the cancel
    sentinel) when no cancel is set."""
    from aiforge_core.runtime import chat_cancel
    sid = 77124
    chat_cancel.start(sid)
    try:
        assert ca._complete_cancellable(lambda r, m, **k: "", "doer", [], sid) == ""
    finally:
        chat_cancel.finish(sid)
