"""Characterization of every stuck / loop guard: the events it yields (their
ORDER and exact text), the nudge it queues for the model, and what it returns.

These pin today's behaviour so the detectors and the response ladder can be
reorganised without changing a word the user or the model sees. Written
against the code BEFORE the refactor; they must stay green unchanged.
"""
import collections
import types

import pytest

from aiforge_core.runtime.chat_agent._turn import _action as A
from aiforge_core.runtime.chat_agent._turn import _escalate as E
from aiforge_core.runtime.chat_agent._turn import _finish as F
from aiforge_core.runtime.chat_agent._turn import _limits as L
from aiforge_core.runtime.chat_agent._turn._progress import progress_fields

GIVE_UP = ("(stopped after repeated attempts: I could not move this forward. "
           "Say \"continue\" and I will pick it up from where the work is on "
           "disk, or tell me what to change.)")


def _st(**over):
    st = types.SimpleNamespace(
        convo=[{"role": "system", "content": "s"},
               {"role": "user", "content": "OBSERVATION: x"}],
        role="doer", action_counts=collections.OrderedDict(),
        read_sigs_seen=set(), recent_outputs=collections.deque(maxlen=3),
        continue_nudges=0, stuck_recoveries=0, board={}, cwd="", session_id=None,
        **progress_fields())
    for k, v in over.items():
        setattr(st, k, v)
    return st


def _drive(gen):
    ev = []
    try:
        while True:
            ev.append(next(gen))
    except StopIteration as stop:
        return ev, stop.value


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in ("AIFORGE_CHAT_PAUSE_ON_STUCK", "AIFORGE_CHAT_STUCK_ESCALATIONS",
                 "AIFORGE_CHAT_STUCK_RESTART", "AIFORGE_CHAT_STUCK_RECOVERIES",
                 "AIFORGE_CHAT_IDENTICAL_REPEATS", "AIFORGE_CHAT_MONOLOGUE_REPEATS",
                 "AIFORGE_CHAT_LOOP_BACKSTOP", "AIFORGE_CHAT_MAX_RECOVERIES"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(E, "_condense", lambda st: None)


def _sys(text):
    return {"type": "thought", "role": "system", "text": text}


# ── escalate ─────────────────────────────────────────────────────────────

def test_escalate_event_text_and_tier_texts():
    st = _st()
    ev, r = _drive(E.escalate(st, "You keep repeating `x` without progress."))
    assert r == "continue"
    assert ev == [_sys("↺ You keep repeating `x` without progress — changing "
                       "approach (try 1), the task continues")]
    assert st.convo[-1]["content"] == (
        "OBSERVATION: x\n\n[loop guard — not the user] You keep repeating `x` "
        "without progress. Step back. In one line say what the last attempts "
        "had in common, then choose an approach you have NOT tried (a "
        "different tool, a different file, a smaller step) and do it now.")
    assert st.reason_boost == 6 and st.stuck_escalations == 1
    _drive(E.escalate(st, "again."))
    assert st.convo[-1]["content"].endswith(
        "Make the smallest change that moves the task forward and do it: read "
        "the one file you need, or write the one edit you are sure of. If a "
        "tool keeps failing, use a different tool for the same goal.")


def test_escalate_wrap_up_text(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_STUCK_ESCALATIONS", "1")
    st = _st()
    assert _drive(E.escalate(st, "a."))[1] == "continue"
    assert _drive(E.escalate(st, "b."))[1] == "continue"       # the wrap-up nudge
    assert st.convo[-1]["content"].endswith(
        "Stop retrying. Write `FINAL:` now: what is done, what remains, and "
        "what you would do next. Be specific.")
    assert _drive(E.escalate(st, "c."))[1] == "wrap_up"


def test_give_up_message_text():
    assert E.give_up_message(_st()) == GIVE_UP


def test_escalate_records_the_failed_approach():
    st = _st()
    _drive(E.escalate(st, "You keep repeating `x` without progress."))
    assert st.failed_approaches and "repeating" in st.failed_approaches[0]


# ── same action (stall guard) ────────────────────────────────────────────

def test_duplicate_read_is_skipped_with_exact_text():
    st = _st()
    st.read_sigs_seen.add("sig")
    ev, r = _drive(A._action_stall_guard(st, "file_read", {}, "sig", True))
    assert r == "continue"
    assert ev == [_sys("⏭ duplicate read skipped (file_read)")]
    assert st.convo[-1] == {"role": "user", "content": (
        "OBSERVATION: [skipped — duplicate] You ALREADY ran this exact read; "
        "its result is above and re-reading wastes a step. "
        + (A._progress_recap(st.convo[:-1]) + ". "
           if A._progress_recap(st.convo[:-1]) else "")
        + "Read a DIFFERENT file you have not read yet, or if you have enough, "
        "WRITE your output now (file_write) or emit FINAL.")}


def _loop(st, sig="run_command|a", n=4):
    out = None
    for _ in range(n):
        ev, out = _drive(A._action_stall_guard(st, "run_command", {}, sig, True))
        if out:
            return ev, out
    return [], None


def test_repeat_recovers_first_with_a_recap_nudge():
    st = _st()
    ev, r = _loop(st)
    assert r == "continue"
    assert ev == [_sys("↺ repeated `run_command` — recap + nudge to continue")]
    nudge = st.convo[-1]["content"]
    assert nudge.startswith("[loop guard — not the user] You already ran "
                            "`run_command` with these exact args and its "
                            "result is ABOVE — repeating it makes no progress. ")
    assert nudge.endswith("Do the NEXT, DIFFERENT step now: act on something "
                          "not yet done (e.g. the next unread file from the "
                          "request), or output `FINAL: <answer>` if everything "
                          "is complete. Do NOT repeat a previous action.")
    assert st.stuck_recoveries == 1 and st.recoveries_total == 1


def test_repeat_after_the_recovery_budget_escalates(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_STUCK_RECOVERIES", "1")
    st = _st()
    assert _loop(st)[1] == "continue"                    # the recap nudge
    ev, r = _loop(st)
    assert r == "continue"
    assert ev == [_sys("↺ You keep repeating `run_command` without progress — "
                       "changing approach (try 1), the task continues")]


def test_repeat_gives_up_once_the_escalations_are_spent(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_STUCK_RECOVERIES", "0")
    monkeypatch.setenv("AIFORGE_CHAT_STUCK_ESCALATIONS", "1")
    st = _st(stuck_escalations=2)
    ev, r = _loop(st)
    assert r == "return"
    assert ev[-2:] == [{"type": "message", "text": GIVE_UP}, {"type": "done"}]
    assert ev[0]["text"].startswith("↺ You keep repeating `run_command`")


def test_repeat_with_pause_on_stuck_asks_the_user(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_STUCK_RECOVERIES", "0")
    monkeypatch.setenv("AIFORGE_CHAT_PAUSE_ON_STUCK", "1")
    st = _st()
    ev, r = _loop(st)
    assert r == "return"
    assert ev == [
        {"type": "message", "awaiting_input": True,
         "text": "I keep trying the same step (`run_command`) without "
                 "progress. I've paused — could you clarify or tell me how "
                 "you'd like me to proceed?"},
        {"type": "done"}]


def test_often_nudge_text():
    assert A._loop_nudge("t", "often", "R") == (
        "[loop guard — not the user] You have run `t` many times in this run "
        "without finishing. Step back: say what the recent results have in "
        "common, then try a different approach — or, if you are blocked, "
        "finish with FINAL and say what blocks you. R.")


def test_identical_result_run_goes_straight_to_the_ladder(monkeypatch):
    from aiforge_core.runtime.chat_agent._turn import _progress as P
    monkeypatch.setenv("AIFORGE_CHAT_STUCK_RECOVERIES", "0")
    st = _st()
    for _ in range(5):
        P.note_identical(st, "sig", {"ok": True, "stdout": ""})
    ev, r = _drive(A._action_stall_guard(st, "run_command", {}, "sig", True))
    assert r == "continue"
    assert ev[0]["text"].startswith("↺ You keep repeating `run_command`")


# ── same output ──────────────────────────────────────────────────────────

def _same_output(st, out="same"):
    ev, r = [], None
    for _ in range(3):
        ev, r = _drive(L._stuck_output_guard(st, out))
    return ev, r


def test_same_output_recovers_with_a_nudge_after_the_assistant_turn():
    st = _st()
    ev, r = _same_output(st)
    assert r == "continue"
    assert ev == [_sys("↺ repeated output — recap + nudge to continue")]
    assert st.convo[-2] == {"role": "assistant", "content": "same"}
    assert st.convo[-1]["content"].startswith(
        "[loop guard — not the user] You repeated the SAME output — that "
        "makes no progress. ")
    assert st.convo[-1]["content"].endswith(
        "Take the NEXT, DIFFERENT step now: act on something not yet done "
        "(e.g. the next unread file), or output `FINAL: <answer>` if the task "
        "is fully complete. Do NOT repeat a previous action.")
    assert len(st.recent_outputs) == 0


def test_same_output_escalates_then_gives_up(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_STUCK_RECOVERIES", "0")
    monkeypatch.setenv("AIFORGE_CHAT_STUCK_ESCALATIONS", "1")
    st = _st()
    ev, r = _same_output(st)
    assert (ev, r) == ([_sys("↺ You keep sending the same reply — changing "
                             "approach (try 1), the task continues")], "continue")
    assert st.convo[-2]["role"] == "assistant"
    st.stuck_escalations = 2
    ev, r = _same_output(st)
    assert r == "return"
    assert ev[-2:] == [{"type": "message", "text": GIVE_UP}, {"type": "done"}]


def test_same_output_with_pause_on_stuck(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_STUCK_RECOVERIES", "0")
    monkeypatch.setenv("AIFORGE_CHAT_PAUSE_ON_STUCK", "1")
    ev, r = _same_output(_st())
    assert r == "return"
    assert ev == [
        {"type": "message", "awaiting_input": True,
         "text": "I seem to be going in circles on this. Could you clarify "
                 "what you'd like me to do, or give a bit more detail? (I "
                 "stopped rather than keep retrying the same thing.)"},
        {"type": "done"}]


# ── idle replies and monologue ───────────────────────────────────────────

def _reply(st, text):
    st.convo.append({"role": "assistant", "content": text})
    return _drive(L._idle_reply_guard(st))


def test_idle_replies_nudge_once_then_escalate():
    st = _st()
    outs = [_reply(st, f"distinct words number {chr(97 + i) * 7}") for i in range(8)]
    assert [o[1] for o in outs[:7]] == ["continue"] * 7 and all(not o[0] for o in outs[:7])
    ev, r = outs[7]
    assert r == "continue"
    assert ev == [_sys("↺ replying without acting — nudge to act or finish")]
    assert st.convo[-1] == {"role": "user", "content": (
        "[loop guard — not the user] Your last replies ran no tool and did "
        "not finish. Either take the next ACTION now, or answer with "
        "`FINAL: <answer>` — or, if you need something from the user, ask ONE "
        "clear question.")}
    outs = [_reply(st, f"another {chr(97 + i) * 9} text") for i in range(8)]
    ev, r = outs[7]
    assert r == "continue"
    assert ev == [_sys("↺ You keep replying without acting — changing approach "
                       "(try 1), the task continues")]


def test_idle_replies_pause_on_stuck(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_PAUSE_ON_STUCK", "1")
    st = _st(idle_trips=1)
    outs = [_reply(st, f"w{i}") for i in range(8)]
    ev, r = outs[7]
    assert r == "return"
    assert ev == [
        {"type": "message", "awaiting_input": True,
         "text": "I keep replying without making progress. I've paused — "
                 "could you tell me what you'd like me to do next?"},
        {"type": "done"}]


def test_monologue_escalates_before_the_idle_limit():
    st = _st()
    assert _reply(st, "I will now look at the file again")[0] == []
    assert _reply(st, "I will now look at the file again!")[0] == []
    ev, r = _reply(st, "I will now look at the file again.")
    assert r == "continue"
    assert ev == [_sys("↺ You keep saying the same thing without acting — "
                       "changing approach (try 1), the task continues")]
    assert st.idle_replies == 0


# ── narration ────────────────────────────────────────────────────────────

def _narrate(st, builder="", **step):
    step = {"kind": "continue", "thought": "", **step}
    return _drive(F._handle_continue_step(st, step, builder, ""))


def test_narration_nudges_twice_then_escalates():
    st = _st()
    ev, r = _narrate(st, thought="T")
    assert (ev, r) == ([{"type": "thought", "text": "T"}], "continue")
    assert st.convo[-1]["content"] == (
        "You described your next step but did NOT emit an ACTION. Continue "
        "now — output the next ACTION (tool call) to make progress, or "
        "`FINAL: <answer>` if you are genuinely done. Do not just narrate.")
    _narrate(st)
    ev, r = _narrate(st)
    assert r == "continue" and st.continue_nudges == 0
    assert ev == [_sys("↺ You keep saying what you will do without doing it — "
                       "changing approach (try 1), the task continues")]


def test_narration_gives_up_when_escalations_are_spent(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_STUCK_ESCALATIONS", "1")
    st = _st(stuck_escalations=2, continue_nudges=2)
    ev, r = _narrate(st)
    assert r == "return"
    assert ev[-2:] == [{"type": "message", "text": GIVE_UP}, {"type": "done"}]


def test_narration_pause_on_stuck_stops_with_the_thought(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_PAUSE_ON_STUCK", "1")
    st = _st(continue_nudges=2)
    ev, r = _narrate(st, thought="my plan")
    assert r == "return"
    assert ev[-2:] == [{"type": "message", "text": "my plan"}, {"type": "done"}]
    st = _st(continue_nudges=2)
    ev, r = _narrate(st, reason="empty_final")
    assert ev[-2] == {"type": "message", "text": (
        "(stopped: I signalled I was done but never wrote the reply. Ask me "
        "to summarise what happened and I'll write it up.)")}


def test_empty_final_nudges():
    st = _st()
    _narrate(st, reason="empty_final")
    assert st.convo[-1]["content"].startswith(
        "You signalled you were finished but wrote no answer")
    st = _st()
    _narrate(st, builder="job", reason="empty_final")
    assert "so nothing was created" in st.convo[-1]["content"]


# ── no progress / same failure (post-tool stop) ──────────────────────────

def _post(st, seen):
    A_note_failure, A_note_step = A.note_failure, A.note_step
    A.note_failure = lambda *a, **k: seen
    A.note_step = lambda *a, **k: None
    try:
        return _drive(A._post_tool(
            st, "run_command", {}, {"ok": False}, "", "sig", 1, True,
            types.SimpleNamespace(skills_md="")))
    finally:
        A.note_failure, A.note_step = A_note_failure, A_note_step


def test_post_tool_stop_escalates_after_the_observation():
    st = _st()
    ev, r = _post(st, ("stop", "same failure again"))
    assert r is None
    assert ev[-1] == _sys("↺ No progress after the warning: same failure "
                          "again — changing approach (try 1), the task "
                          "continues")
    assert st.convo[-1]["content"].startswith("OBSERVATION: ")
    assert "[loop guard — not the user] No progress after the warning: " \
        "same failure again" in st.convo[-1]["content"]


def test_post_tool_stop_gives_up_when_spent(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_STUCK_ESCALATIONS", "1")
    st = _st(stuck_escalations=2)
    ev, r = _post(st, ("stop", "same failure again"))
    assert r == "return"
    assert ev[-2:] == [{"type": "message", "text": GIVE_UP}, {"type": "done"}]


def test_post_tool_stop_pause_on_stuck(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_PAUSE_ON_STUCK", "1")
    ev, r = _post(_st(), ("stop", "same failure again"))
    assert r == "return"
    assert ev[-3:] == [_sys("⛔ no progress after the warning — pausing"),
                       {"type": "message", "awaiting_input": True,
                        "text": "same failure again"}, {"type": "done"}]


def test_post_tool_nudge_rides_on_the_observation():
    st = _st()
    ev, r = _post(st, ("nudge", "WARN"))
    assert r is None
    assert ev[-1] == _sys("↺ going round without progress — asking why before "
                          "another try")
    assert st.convo[-1]["content"].endswith("\nWARN")


# ── policy: env is re-read on every load ─────────────────────────────────

def test_policy_rereads_the_environment(monkeypatch):
    from aiforge_core.runtime.stuck_policy import Policy
    assert Policy.load().backstop == 30
    monkeypatch.setenv("AIFORGE_CHAT_LOOP_BACKSTOP", "7")
    assert Policy.load().backstop == 7
    monkeypatch.setenv("AIFORGE_CHAT_LOOP_BACKSTOP", "-3")
    assert Policy.load().backstop == 30
    monkeypatch.setenv("AIFORGE_CHAT_LOOP_BACKSTOP", "junk")
    assert Policy.load().backstop == 30


@pytest.mark.parametrize("name,field,raw,expected", [
    ("AIFORGE_CHAT_MAX_RECOVERIES", "max_recoveries", "0", 30),
    ("AIFORGE_CHAT_STUCK_RECOVERIES", "stuck_recoveries", "-4", 0),
    ("AIFORGE_CHAT_STUCK_RECOVERIES", "stuck_recoveries", "x", 3),
    ("AIFORGE_CHAT_IDENTICAL_REPEATS", "identical_repeats", "2", 2),
    ("AIFORGE_CHAT_MONOLOGUE_REPEATS", "monologue_repeats", "0", 0),
    ("AIFORGE_CHAT_MONOLOGUE_SIMILARITY", "monologue_similarity", "0.1", 0.5),
    ("AIFORGE_CHAT_MONOLOGUE_SIMILARITY", "monologue_similarity", "7", 1.0),
    ("AIFORGE_CHAT_MONOLOGUE_SIMILARITY", "monologue_similarity", "z", 0.85),
    ("AIFORGE_CHAT_PAUSE_ON_STUCK", "pause_on_stuck", "On", True),
    ("AIFORGE_CHAT_PAUSE_ON_STUCK", "pause_on_stuck", "", False),
    ("AIFORGE_CHAT_STUCK_ESCALATIONS", "stuck_escalations", "-1", 0),
    ("AIFORGE_CHAT_STUCK_RESTART", "stuck_restart", "off", False),
    ("AIFORGE_CHAT_CONTEXT_ERROR_RESTART", "context_error_restart", " NO ", False),
    ("AIFORGE_CHAT_GOAL_LOOP", "goal_loop", "0", False),
    ("AIFORGE_NO_PROGRESS_STEPS", "no_progress_steps", "3", 8),
    ("AIFORGE_NO_PROGRESS_STEPS", "no_progress_steps", "x", 25),
    ("AIFORGE_SAME_FAILURE_LIMIT", "same_failure_limit", "1", 2),
    ("AIFORGE_STUCK_REASON_STEPS", "reason_steps", "-1", 0),
    ("AIFORGE_TOOL_REPEAT_LIMIT", "tool_repeat_limit", "0", 0),
    ("AIFORGE_PLATEAU_REPLANS", "plateau_replans", "", 2),
    ("AIFORGE_NO_EDIT_ITERS", "no_edit_iters", "5", 5),
])
def test_policy_parsing_matches_the_old_per_module_parsers(
        monkeypatch, name, field, raw, expected):
    from aiforge_core.runtime.stuck_policy import Policy
    monkeypatch.setenv(name, raw)
    assert getattr(Policy.load(), field) == expected
