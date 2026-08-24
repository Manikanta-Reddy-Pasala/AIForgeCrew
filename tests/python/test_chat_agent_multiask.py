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
    assert docks
    assert len(docks[0]["items"]) == 3
    assert docks[0]["items"][0]["slug"] == "part-1"
    # EVERY item carries `goal`. The UI's Tasks dock reads that field, and this
    # path used to emit `title` alone — so expanding the dock threw "Cannot
    # read properties of undefined (reading 'length')" and the error boundary
    # replaced the whole chat view. Five of the six producers use `goal`; this
    # was the one that drifted.
    for it in docks[0]["items"]:
        assert it.get("goal"), f"subtask without a goal: {it}"
    ups = [(e["slug"], e["status"]) for e in evs if e["type"] == "subtask_update"]
    assert ("part-1", "done") in ups          # agent flipped it via the tool
    # FINAL closes out the rest so nothing lingers pending
    assert ("part-2", "done") in ups
    assert ("part-3", "done") in ups


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


# ── an empty completion is not an answer ────────────────────────────────

def test_bare_action_final_is_not_the_answer(tmp_path):
    """A model that emits `ACTION: FINAL` with nothing after it has signalled
    completion without writing one. The parser used to hand the MARKER back as
    the final text, so the chat ended with the literal words "ACTION: FINAL"
    after the agent had done all the work."""
    from aiforge_core.runtime.chat_agent._prompt import _parse
    assert _parse("ACTION: FINAL")["kind"] == "continue"
    assert _parse("ACTION: final\nARGS_JSON: {}")["kind"] == "continue"
    # …and the model's private reasoning is not an answer either.
    p = _parse("THOUGHT: done\nACTION: FINAL")
    assert p["kind"] == "continue"
    assert p["thought"] == "done"
    # Real answers still pass through, in both spellings.
    assert _parse("FINAL: shipped it")["text"] == "shipped it"
    assert _parse("ACTION: FINAL\nShipped it.")["text"] == "Shipped it."
    assert _parse('ACTION: final\nARGS_JSON: {"text": "Shipped it."}'
                  )["text"] == "Shipped it."


def test_the_answer_after_the_marker_is_never_edited():
    """The marker/THOUGHT stripping applies ONLY to the whole-turn fallback —
    the slice AFTER the marker is the user's answer verbatim. Running the
    filters over it ate `thought:` and `action: done` lines out of a YAML block
    the answer was quoting."""
    from aiforge_core.runtime.chat_agent._prompt import _parse
    answer = ("Here is the config:\n```yaml\nthought: keep\naction: done\n"
              "```\nAll set.")
    assert _parse(f"ACTION: FINAL\n{answer}")["text"] == answer
    # A code block is a legitimate whole answer, too.
    assert _parse("ACTION: FINAL\n```bash\nls\n```")["text"] == "```bash\nls\n```"


def test_an_empty_completion_is_nudged_into_a_real_answer(tmp_path):
    """End to end: the turn does NOT end on the marker — the loop asks for the
    answer, and the answer is what the user gets."""
    fn = _scripted([
        'ACTION: file_write\nARGS_JSON: {"path": "a.txt", "content": "hi"}',
        "ACTION: FINAL",                       # signalled done, wrote nothing
        "FINAL: Patched the script and verified it runs.",
    ])
    evs = _collect(ca.run_chat_agent(
        [{"role": "user", "content": "patch the script"}],
        cwd=str(tmp_path), complete_fn=fn))
    msgs = [e["text"] for e in evs if e["type"] == "message"]
    assert msgs, "the turn produced no answer at all"
    assert "ACTION: FINAL" not in " ".join(msgs)
    assert "Patched the script and verified it runs." in " ".join(msgs)


def test_an_empty_completion_does_not_double_charge_the_step_budget(tmp_path):
    """The loop head already counts the iteration. Charging again made one
    nudge cost two steps, so a 6-step Quick turn hit its cap and reported a
    stop instead of the answer."""
    fn = _scripted([
        'ACTION: file_read\nARGS_JSON: {"path": "a.txt"}',
        "ACTION: FINAL",                       # nudge #1
        "FINAL: Read it — it says hi.",
    ])
    (tmp_path / "a.txt").write_text("hi")
    evs = _collect(ca.run_chat_agent(
        [{"role": "user", "content": "what does a.txt say"}],
        cwd=str(tmp_path), complete_fn=fn, max_steps=3, session_id=None))
    msgs = " ".join(e["text"] for e in evs if e["type"] == "message")
    assert "Read it — it says hi." in msgs
    assert "step budget" not in msgs
    assert "safety cap" not in msgs


def test_a_builder_is_pushed_at_its_finalize_tool_not_away_from_it(tmp_path):
    """A builder's answer is an ARTIFACT. The generic nudge says "reply with
    FINAL, do not emit ACTION" — the exact opposite of what it must do, and
    the turn ended claiming success with nothing created."""
    seen: list = []

    def fn(role, messages, **kw):
        seen.append(messages)
        return "ACTION: FINAL"

    list(ca.run_chat_agent([{"role": "user", "content": "make a skill"}],
                           cwd=str(tmp_path), complete_fn=fn,
                           builder="skill", session_id=None))
    nudges = "\n".join(m.get("content") or "" for msgs in seen for m in msgs
                       if m.get("role") == "user")
    assert "learn_skill" in nudges
    assert "Do not emit ACTION" not in nudges


def test_the_give_up_line_does_not_claim_the_work_is_done(tmp_path):
    """`text_doer` and `analysis_pipeline` treat a final message that does not
    start with "(stopped:" as a CLEAN outcome, so claiming completion here
    would write a false success into the pipeline's record — and the recap it
    used to carry tallied the FINAL markers themselves."""
    fn = _scripted(["ACTION: FINAL"] * 6)
    evs = _collect(ca.run_chat_agent(
        [{"role": "user", "content": "do it"}], cwd=str(tmp_path),
        complete_fn=fn, session_id=None))
    msgs = " ".join(e["text"] for e in evs if e["type"] == "message")
    assert msgs.strip().startswith("(stopped:")
    assert "finished the work" not in msgs
    assert "FINAL×" not in msgs
    assert "ACTION: FINAL" not in msgs
