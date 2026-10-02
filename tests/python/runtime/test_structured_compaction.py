"""Structured compaction: a typed summary, a restart on the second condense, a
system message that never changes, a reply reserve that scales, and two more
stuck detectors (monologue, repeated context-window errors)."""
import json
import threading
import time
from types import SimpleNamespace

import pytest

from aiforge_core.runtime.chat_agent._context import (
    _compaction as comp,
    _note,
    _structured as S,
    _summary_bg,
    _window as W,
)
from aiforge_core.runtime.chat_agent._turn import _completion as CP
from aiforge_core.runtime.chat_agent._turn import _escalate as E
from aiforge_core.runtime.chat_agent._turn import _limits as L
from aiforge_core.runtime.chat_agent._turn import _progress as P
from aiforge_core.runtime.chat_agent._turn import _tasks


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_CHAT_CONTEXT_BUDGET_CHARS", "2000")
    for k in ("AIFORGE_COMPACT_STRUCTURED", "AIFORGE_STABLE_PREFIX",
              "AIFORGE_COMPACT_RESTART", "AIFORGE_CHAT_PAUSE_ON_STUCK",
              "AIFORGE_CHAT_STUCK_ESCALATIONS", "AIFORGE_CHAT_STUCK_RESTART",
              "AIFORGE_CHAT_MONOLOGUE_REPEATS", "AIFORGE_OUTPUT_RESERVE_FRAC",
              "AIFORGE_CHAT_CONTEXT_ERROR_RESTART"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AIFORGE_COMPACT_MODE", "heuristic")
    _summary_bg.reset()
    yield
    _summary_bg.reset()


def _long(n=30, tag="x", first="build the UNIQUEALPHA exporter"):
    convo = [{"role": "system", "content": "SYSTEM PROMPT " + "S" * 100},
             {"role": "user", "content": first}]
    for _ in range(n):
        convo.append({"role": "assistant",
                      "content": "THOUGHT: t\nACTION: file_read\nARGS_JSON: {}"})
        convo.append({"role": "user", "content": "OBSERVATION: " + tag * 200})
    return convo


def _grow(convo, n=20, tag="y"):
    convo = list(convo)
    for _ in range(n):
        convo.append({"role": "assistant",
                      "content": "THOUGHT: t\nACTION: grep\nARGS_JSON: {}"})
        convo.append({"role": "user", "content": "OBSERVATION: " + tag * 200})
    return convo


def _wait(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


# ── 1. typed summary ───────────────────────────────────────────────────────

_RECORD = {
    "goal": "add the CSV exporter",
    "done_and_verified": ["wrote exporter.py", "test_export passes"],
    "pending": ["wire the CLI flag"],
    "files_changed": ["src/exporter.py", "tests/test_export.py"],
    "failing_tests_errors": ["AssertionError: 3 != 4 in test_cli_flag"],
    "decisions": ["use csv.DictWriter: handles quoting"],
    "failed_approaches": ["pandas.to_csv: not installed"],
    "next_step": "add --csv to cli.py",
}


def test_a_typed_summary_parses_from_json_fenced_json_and_labelled_lines():
    rec = S.parse(json.dumps(_RECORD))
    assert rec["goal"] == "add the CSV exporter"
    assert rec["failing_tests_errors"] == ["AssertionError: 3 != 4 in test_cli_flag"]
    assert rec["files_changed"][0] == "src/exporter.py"
    fenced = "Here you go:\n```json\n" + json.dumps(_RECORD) + "\n```"
    assert S.parse(fenced) == rec
    labelled = ("GOAL: add the CSV exporter\nDONE AND VERIFIED:\n- wrote exporter.py\n"
                "- test_export passes\nPENDING:\n- wire the CLI flag\n"
                "FAILING TESTS / ERRORS (verbatim):\n- AssertionError: 3 != 4\n"
                "NEXT STEP: add --csv to cli.py")
    lab = S.parse(labelled)
    assert lab["done_and_verified"] == ["wrote exporter.py", "test_export passes"]
    assert lab["next_step"] == "add --csv to cli.py"
    assert lab["failing_tests_errors"] == ["AssertionError: 3 != 4"]
    # aliases and a string where a list belongs
    alias = S.parse(json.dumps({"task": "t", "done": "a\n- b", "errors": "boom"}))
    assert alias["goal"] == "t" and alias["done_and_verified"] == ["a", "b"]
    assert alias["failing_tests_errors"] == ["boom"]


def test_a_reply_without_the_fields_is_refused():
    assert S.parse("- edited foo.py\n- ran the tests") is None
    assert S.parse("") is None and S.parse(None) is None
    assert S.parse("{not json") is None
    assert S.parse(json.dumps({"goal": "only one field"})) is None     # too thin
    assert S.parse(json.dumps({"files_changed": ["a"], "decisions": ["b"]})) is None
    assert S.parse(json.dumps(["a", "b"])) is None


def test_the_rendered_summary_is_labelled_and_bounded():
    big = dict(_RECORD, decisions=["d" * 5000] * 50,
               failing_tests_errors=["e" * 5000] * 50)
    text = S.render(S.parse(json.dumps(big)))
    assert "GOAL: add the CSV exporter" in text
    assert "FAILING TESTS / ERRORS (verbatim):" in text
    assert "NEXT STEP: add --csv to cli.py" in text
    assert len(text) < comp._SUMMARY_MAX_CHARS + 6000        # bounded per field
    assert text.count("\n- ") <= 3 * S._MAX_ITEMS + 8
    assert S.render(S.parse(json.dumps(_RECORD))).startswith("GOAL:")


def test_the_summary_prompt_asks_for_every_field(monkeypatch):
    msgs = comp._summary_messages(_long()[1:8], prior="")
    for key in S.FIELDS:
        assert key in msgs[0]["content"]
    monkeypatch.setenv("AIFORGE_COMPACT_STRUCTURED", "0")
    assert comp._summary_messages(_long()[1:8])[0]["content"] == comp._COMPACT_SYS


def _bg_run(monkeypatch, reply):
    monkeypatch.setenv("AIFORGE_COMPACT_MODE", "llm")
    out = comp._compact_convo(_long(), keep_recent=8, run_key="run-s",
                              complete_fn=lambda role, msgs: reply)
    assert _wait(lambda: _summary_bg.pending("run-s") is None)
    time.sleep(0.05)
    return out, comp._compact_convo(out, keep_recent=8, run_key="run-s",
                                    complete_fn=lambda role, msgs: reply)


def test_a_typed_reply_is_spliced_into_the_note(monkeypatch):
    out, nxt = _bg_run(monkeypatch, json.dumps(_RECORD))
    note = nxt[1]["content"]
    assert "NEXT STEP: add --csv to cli.py" in note
    assert "Work done so far: file_read" in note           # the tally stays
    assert nxt[0] == out[0]


def test_an_unparseable_reply_falls_back_to_the_breadcrumb(monkeypatch):
    out, nxt = _bg_run(monkeypatch, "- edited foo.py\n- ran the tests\nrambling")
    assert nxt[1]["content"] == out[1]["content"]          # nothing spliced
    assert "Summary of what happened" not in nxt[1]["content"]
    assert "Work done so far: file_read" in nxt[1]["content"]


def test_the_free_text_summary_still_works_when_the_switch_is_off(monkeypatch):
    monkeypatch.setenv("AIFORGE_COMPACT_STRUCTURED", "0")
    _out, nxt = _bg_run(monkeypatch, "- edited foo.py")
    assert "- edited foo.py" in nxt[1]["content"]


# ── 3. a stable prefix ─────────────────────────────────────────────────────

def test_the_system_message_is_byte_identical_after_two_condenses():
    convo = _long()
    sys0 = convo[0]["content"]
    st = SimpleNamespace(board=_tasks.seed_board(["a", "b"]))
    out1 = comp._compact_convo(convo, keep_recent=8, pin="ORIGINAL TASK: go")
    _tasks.pin_board(out1, st.board)
    assert out1[0]["content"] == sys0
    out2 = comp._compact_convo(_grow(out1), keep_recent=8,
                               pin="ORIGINAL TASK: go\nFILES CHANGED SO FAR: x.py")
    _tasks.pin_board(out2, st.board)
    out3 = comp._compact_convo(_grow(out2), keep_recent=8, force=True,
                               pin="ORIGINAL TASK: go\nFILES CHANGED SO FAR: y.py")
    assert out2[0]["content"] == sys0 == out3[0]["content"]
    assert out2[0] is convo[0] and out3[0] is convo[0]
    for out in (out1, out2, out3):
        assert _note.is_note(out[1])
        assert comp._CONDENSE_OPEN not in out[0]["content"]
        assert comp._GOAL_PIN_OPEN not in out[0]["content"]
    note = out3[1]["content"]
    assert note.count(comp._CONDENSE_OPEN) == 1             # one block, not stacked
    assert note.count(comp._GOAL_PIN_OPEN) == 1
    assert "(condense #3)" in note
    assert "FILES CHANGED SO FAR: y.py" in note             # pin replaced
    assert note.count(_tasks._BOARD_OPEN) == 1 and "[ ] part-1: a" in note
    assert "UNIQUEALPHA" in note                            # the asks roll forward


def test_roles_alternate_after_a_condense():
    out = comp._compact_convo(_long(), keep_recent=8)
    roles = [m["role"] for m in out]
    assert roles[:3] == ["system", "user", "assistant"]     # note + ack
    assert not any(roles[i] == roles[i + 1] != "system" for i in range(len(roles) - 1))
    assert roles[-1] == "user"
    # a tail that opens on an assistant turn follows the note directly
    native = [{"role": "system", "content": "S"}, {"role": "user", "content": "go"}]
    for i in range(12):
        native.append({"role": "assistant", "content": "",
                       "tool_calls": [{"id": f"c{i}"}]})
        native.append({"role": "tool", "tool_call_id": f"c{i}", "content": "r" * 400})
    n = comp._compact_convo(native, keep_recent=4)
    nroles = [m["role"] for m in n]
    assert nroles[:2] == ["system", "user"] and nroles[2] != "user"
    assert _note.is_note(n[1])


def test_the_board_pins_into_the_note_not_the_system_message():
    out = comp._compact_convo(_long(), keep_recent=8)
    sys0 = out[0]["content"]
    st = SimpleNamespace(board=_tasks.seed_board(["a"]), batch_unread=False,
                         convo=out)
    L._after_condense(st, 0, before=len(out) + 5)
    assert st.convo[0]["content"] == sys0
    assert _tasks._BOARD_OPEN in st.convo[1]["content"]


def test_the_note_is_not_read_as_the_users_words_for_tool_selection():
    from aiforge_core.runtime.chat_agent._native_select import _convo_text
    note = _note.build("<<AIFORGE_PINNED_GOAL>>\nORIGINAL TASK: see jira "
                       "https://x.example\n<</AIFORGE_PINNED_GOAL>>")
    assert _convo_text([{"role": "system", "content": "s"}, note,
                        {"role": "user", "content": "rename foo"}]) == "rename foo"


def test_stable_prefix_off_restores_the_old_layout(monkeypatch):
    monkeypatch.setenv("AIFORGE_STABLE_PREFIX", "0")
    convo = _long()
    out = comp._compact_convo(convo, keep_recent=8)
    assert comp._CONDENSE_OPEN in out[0]["content"]
    assert not any(_note.is_note(m) for m in out)
    assert out[0]["content"] != convo[0]["content"]


def test_a_summary_is_spliced_into_the_note_and_the_system_message_stays(monkeypatch):
    monkeypatch.setenv("AIFORGE_COMPACT_STRUCTURED", "0")
    out, nxt = _bg_run(monkeypatch, "- edited foo.py")
    assert nxt[0] == out[0] and nxt[2:] == out[2:]
    assert "- edited foo.py" in nxt[1]["content"]


def _names(tools):
    return [(t.get("function") or {}).get("name") for t in tools]


def test_the_native_tool_list_is_deterministic_and_only_grows(monkeypatch):
    from aiforge_core.llm import client
    from aiforge_core.runtime.chat_agent import _native
    _native.reset_native_cache()
    monkeypatch.setenv("AIFORGE_CHAT_GATE_TOOLS", "0")    # Jira need not be configured
    monkeypatch.setattr(_native, "_model_for", lambda role: "m-stable")
    sent = []

    def _raw(role, msgs, tools=None, tool_choice=None):
        sent.append(_names(tools or []))
        return {"role": "assistant", "content": "FINAL: ok"}
    monkeypatch.setattr(client, "complete_raw", _raw)
    sysm = {"role": "system", "content": "s"}
    plain = [sysm, {"role": "user", "content": "rename foo to bar"}]
    jira = plain + [{"role": "assistant", "content": "ok"},
                    {"role": "user", "content": "now show my jira tickets"}]
    fn = _native.make_native_complete_fn(session_id=None)
    fn("chat", plain)
    fn("chat", plain)
    assert sent[0] == sent[1]                               # same input, same list
    fn("chat", jira)
    assert sent[2][:len(sent[0])] == sent[0]                # grew: old prefix intact
    assert len(sent[2]) > len(sent[0]) and any(n.startswith("jira_") for n in sent[2])
    # the Jira message is condensed away: the list does not shrink or reorder
    fn("chat", plain)
    assert sent[3] == sent[2]
    # a fresh run replaying the same steps sends the same lists, in order
    fn2 = _native.make_native_complete_fn(session_id=None)
    fn2("chat", plain)
    fn2("chat", jira)
    fn2("chat", plain)
    assert sent[4:7] == [sent[0], sent[2], sent[3]]
    assert len(set(sent[2])) == len(sent[2])                # no duplicates


# ── 2. the second condense restarts from a handoff ─────────────────────────

def _state(convo):
    return SimpleNamespace(
        convo=convo, goal="build the UNIQUEALPHA exporter",
        board={"part-1": {"title": "write exporter", "status": "done"},
               "part-2": {"title": "wire the CLI", "status": "pending"}},
        file_hashes={"/w/exporter.py": "h"}, failed_approaches=[])


def test_the_first_condense_is_a_breadcrumb_and_the_second_restarts():
    st = _state(_long())
    out1 = comp._compact_convo(st.convo, keep_recent=8, handoff_st=st)
    note1 = out1[1]["content"]
    assert "auto-condensed" in note1 and "[HANDOFF" not in note1
    st.convo = _grow(out1)
    out2 = comp._compact_convo(st.convo, keep_recent=8, handoff_st=st)
    note2 = out2[1]["content"]
    assert "[HANDOFF (condense #2)" in note2
    assert "GOAL: build the UNIQUEALPHA exporter" in note2
    assert "DONE (verified): write exporter" in note2
    assert "NEXT: wire the CLI" in note2
    assert "/w/exporter.py" in note2
    assert "Work done so far" not in note2                  # not another breadcrumb
    assert note2.count(comp._CONDENSE_OPEN) == 1
    assert "UNIQUEALPHA" in note2 and "memory_lookup" in note2
    assert out2[0] == out1[0]
    # a third condense keeps restarting (one record, never a stack)
    out3 = comp._compact_convo(_grow(out2), keep_recent=8, handoff_st=st)
    assert out3[1]["content"].count(comp._CONDENSE_OPEN) == 1
    assert "[HANDOFF (condense #3)" in out3[1]["content"]
    # the recent tail is kept verbatim (a restart does not blind the model)
    assert out2[-1] == st.convo[-1]


def test_without_run_state_the_second_condense_stacks_as_before():
    out1 = comp._compact_convo(_long(), keep_recent=8)
    out2 = comp._compact_convo(_grow(out1), keep_recent=8)
    assert "(condense #2)" in out2[1]["content"]
    assert "[HANDOFF" not in out2[1]["content"]


def test_the_restart_on_condense_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("AIFORGE_COMPACT_RESTART", "0")
    st = _state(_long())
    out1 = comp._compact_convo(st.convo, keep_recent=8, handoff_st=st)
    out2 = comp._compact_convo(_grow(out1), keep_recent=8, handoff_st=st)
    assert "[HANDOFF" not in out2[1]["content"]
    assert "Work done so far" in out2[1]["content"]


def test_a_failing_handoff_falls_back_to_the_breadcrumb(monkeypatch):
    from aiforge_core.runtime import handoff

    def boom(_st):
        raise RuntimeError("no state")
    monkeypatch.setattr(handoff, "build_chat", boom)
    st = _state(_long())
    out1 = comp._compact_convo(st.convo, keep_recent=8, handoff_st=st)
    out2 = comp._compact_convo(_grow(out1), keep_recent=8, handoff_st=st)
    assert "Work done so far" in out2[1]["content"]


def test_the_stuck_restart_also_leaves_the_system_message_alone():
    st = _state([{"role": "system", "content": "SYSTEM RULES"},
                 {"role": "user", "content": "build the UNIQUEALPHA exporter"},
                 {"role": "assistant", "content": "ACTION: run_command\nARGS_JSON: {}"},
                 {"role": "user", "content": "OBSERVATION: Traceback\nValueError: x"}])
    st.role = "doer"
    assert E.restart_with_handoff(st)
    assert st.convo[0]["content"] == "SYSTEM RULES"
    assert _note.is_note(st.convo[1]) and "[HANDOFF" in st.convo[1]["content"]
    assert _tasks._BOARD_OPEN in st.convo[1]["content"]
    # a later condense carries the pinned goal and counts the restart
    st.convo = _grow(st.convo)
    out = comp._compact_convo(st.convo, keep_recent=8, handoff_st=st)
    assert "UNIQUEALPHA" in out[1]["content"]
    assert out[0]["content"] == "SYSTEM RULES"


# ── 4. the reply reserve ───────────────────────────────────────────────────

@pytest.fixture
def window(monkeypatch):
    monkeypatch.delenv("AIFORGE_CHAT_CONTEXT_BUDGET_CHARS", raising=False)
    monkeypatch.delenv("AIFORGE_CTX_HISTORY_FRACTION", raising=False)
    monkeypatch.setattr(W, "_window_tokens", lambda role=None: 131072)
    from aiforge_core.config import runtime_settings
    monkeypatch.setattr(runtime_settings, "get",
                        lambda k, *a, **kw: 4096 if k == "max_output_tokens" else 0)
    monkeypatch.setattr(W, "_RESERVE_HOOKS", [])
    return 131072


def test_the_default_reserve_is_the_output_cap_as_before(window):
    win_chars = window * 4
    expect = min(int(win_chars * 0.8), win_chars - 4096 * 4) - 5000
    assert W._ctx_budget_chars("chat", sys_chars=5000) == expect


def test_a_reasoning_allowance_shrinks_the_history_budget(window):
    base = W._ctx_budget_chars("chat", sys_chars=5000)
    boosted = W._ctx_budget_chars("chat", sys_chars=5000,
                                  extra_reserve_tokens=60_000)
    assert boosted < base
    # a boosted step must not overflow: system + history + reply <= window
    assert 5000 + boosted + (4096 + 60_000) * 4 <= window * 4


def test_the_reserve_is_capped_to_a_window_fraction(window):
    base = W._ctx_budget_chars("chat", sys_chars=0)
    huge = W._ctx_budget_chars("chat", sys_chars=0, extra_reserve_tokens=10**7)
    # at most half the window is reserved, so history keeps real room
    assert W._CTX_BUDGET_FLOOR_CHARS <= huge <= base
    assert huge == window * 4 - int(window * 0.5) * 4


def test_a_registered_hook_adds_to_the_reserve(window):
    base = W._ctx_budget_chars("chat", sys_chars=5000)
    calls = []

    def hook(role):
        calls.append(role)
        return 50_000
    W.register_reserve_hook(hook)
    W.register_reserve_hook(hook)                      # idempotent
    try:
        assert W._ctx_budget_chars("chat", sys_chars=5000) < base
        assert calls and W._RESERVE_HOOKS.count(hook) == 1
        W.register_reserve_hook(lambda role: 1 / 0)    # a broken hook is ignored
        assert W._ctx_budget_chars("chat", sys_chars=5000) < base
    finally:
        W._RESERVE_HOOKS.clear()
    assert W._ctx_budget_chars("chat", sys_chars=5000) == base


# ── 5a. monologue ──────────────────────────────────────────────────────────

class _St:
    def __init__(self):
        self.convo = [{"role": "system", "content": "SYS"},
                      {"role": "user", "content": "fix the bug"}]
        self.role = "doer"
        self.goal = "fix the bug"
        self.board = {}
        self.file_hashes = {}
        self.read_sigs_seen = set()
        self.recent_outputs = []


def _drive(gen):
    evs = []
    try:
        while True:
            evs.append(next(gen))
    except StopIteration as stop:
        return stop.value, evs


def test_three_reworded_replies_without_a_tool_are_a_monologue():
    st = _St()
    assert not P.note_monologue(st, "I think the issue is in the parser, so I will look at it.")
    assert not P.note_monologue(st, "I think the issue is in the parser; so I will look at it!")
    assert P.note_monologue(st, "I think the issue is in the Parser so I will look at it now")
    P.reset_monologue(st)
    assert not P.note_monologue(st, "first thing")


def test_different_replies_are_not_a_monologue():
    st = _St()
    for t in ("I will read the parser module first.",
              "The tokenizer handles quotes badly; patching it next.",
              "Tests are green now, writing the summary."):
        assert not P.note_monologue(st, t)


def test_the_monologue_check_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_MONOLOGUE_REPEATS", "0")
    st = _St()
    assert not any(P.note_monologue(st, "same same same") for _ in range(5))


def test_a_monologue_routes_to_escalate_before_the_idle_limit():
    st = _St()
    said = "Let me think about this problem again. I should look at the parser."
    results = []
    for _ in range(3):
        st.convo.append({"role": "assistant", "content": said})
        results.append(_drive(L._idle_reply_guard(st)))
    assert results[0][0] == "continue" and results[1][0] == "continue"
    value, evs = results[2]
    assert value == "continue"
    assert any("changing approach" in e.get("text", "") for e in evs)
    assert st.stuck_escalations == 1
    assert "same thing" in st.convo[-1]["content"]          # the nudge says why
    assert st.idle_replies == 0


def test_a_tool_between_replies_is_not_a_monologue():
    st = _St()
    said = "Let me think about this problem again."
    for _ in range(2):
        st.convo.append({"role": "assistant", "content": said})
        assert _drive(L._idle_reply_guard(st))[0] == "continue"
    st.monologue.clear()                 # what the loop does when a tool runs
    st.convo.append({"role": "assistant", "content": said})
    value, evs = _drive(L._idle_reply_guard(st))
    assert value == "continue" and not getattr(st, "stuck_escalations", 0)


# ── 5b. repeated context-window errors ─────────────────────────────────────

def test_a_context_window_error_is_recognised():
    from aiforge_core.llm import model_outage as MO
    assert MO.is_context_overflow(RuntimeError(
        "400: This model's maximum context length is 8192 tokens"))
    assert MO.is_context_overflow(RuntimeError("request exceeds the available context size"))
    assert MO.is_context_overflow(RuntimeError("ContextWindowExceededError: too big"))
    assert not MO.is_context_overflow(RuntimeError("connection refused"))
    assert not MO.is_context_overflow(None)


def test_the_same_prompt_over_the_window_twice_restarts_from_a_handoff(monkeypatch):
    monkeypatch.setattr("aiforge_core.runtime.run_interrupt.pause",
                        lambda *a, **k: None)
    st = _St()
    for i in range(12):
        st.convo.append({"role": "assistant", "content": "ACTION: file_read\nARGS_JSON: {}"})
        st.convo.append({"role": "user", "content": "OBSERVATION: " + "x" * 3000})
    sent = []

    def complete(role, convo):
        sent.append(sum(len(str(m.get("content"))) for m in convo))
        if len(convo) > 3:
            raise RuntimeError("400 maximum context length is 8192 tokens, request has 12000")
        return "FINAL: ok"

    gen = CP._retry_completion(
        complete, "doer", st.convo, None,
        RuntimeError("400 maximum context length is 8192 tokens, request has 12000"),
        None, None, None, wait_s=None, st=st)
    out, evs = _drive(gen)
    assert out == "FINAL: ok"
    assert len(sent) == 2                       # one same-prompt retry, then smaller
    assert sent[1] < sent[0] / 4
    assert _note.is_note(st.convo[1]) and "[HANDOFF" in st.convo[1]["content"]
    assert any("context window" in e.get("text", "") for e in evs)


def test_context_error_restart_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_CONTEXT_ERROR_RESTART", "0")
    monkeypatch.setattr("aiforge_core.runtime.run_interrupt.pause",
                        lambda *a, **k: None)
    st = _St()
    for _ in range(12):
        st.convo.append({"role": "assistant", "content": "ACTION: file_read\nARGS_JSON: {}"})
        st.convo.append({"role": "user", "content": "OBSERVATION: " + "x" * 3000})
    monkeypatch.setenv("AIFORGE_CHAT_CONTEXT_BUDGET_CHARS", "6000")

    def complete(role, convo):
        if sum(len(str(m.get("content"))) for m in convo) > 9000:
            raise RuntimeError("prompt is too long")
        return "FINAL: ok"

    out, evs = _drive(CP._retry_completion(
        complete, "doer", st.convo, None, RuntimeError("prompt is too long"),
        None, None, None, wait_s=None, st=st))
    assert out == "FINAL: ok"
    assert not any("[HANDOFF" in str(m.get("content")) for m in st.convo)   # condensed instead


def test_one_overflow_then_success_changes_nothing(monkeypatch):
    monkeypatch.setattr("aiforge_core.runtime.run_interrupt.pause",
                        lambda *a, **k: None)
    st = _St()
    before = list(st.convo)
    out, _evs = _drive(CP._retry_completion(
        lambda role, convo: "FINAL: ok", "doer", st.convo, None,
        RuntimeError("prompt is too long"), None, None, None, wait_s=None, st=st))
    assert out == "FINAL: ok" and st.convo == before
