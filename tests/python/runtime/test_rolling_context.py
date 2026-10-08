"""Measured characters-per-token for the context budget, and the chat
transcript carried between messages."""
from __future__ import annotations

import json

import pytest

from aiforge_core.llm import ctx_ratio
from aiforge_core.runtime import chat_transcript
from aiforge_core.runtime.chat_agent._context import _window


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    ctx_ratio.reset()
    yield
    ctx_ratio.reset()


def _msgs(chars: int) -> list:
    return [{"role": "system", "content": "s" * (chars // 2)},
            {"role": "user", "content": "u" * (chars - chars // 2)}]


# ── ctx_ratio ────────────────────────────────────────────────────────────────

def test_ratio_on_by_default_and_switchable(monkeypatch):
    monkeypatch.delenv("AIFORGE_CTX_MEASURED", raising=False)
    ctx_ratio.note("doer", _msgs(30_000), 10_000)
    assert ctx_ratio.chars_per_token("doer") == pytest.approx(3.0)
    monkeypatch.setenv("AIFORGE_CTX_MEASURED", "0")
    assert ctx_ratio.chars_per_token("doer") is None


def test_ratio_from_another_model_is_not_used(monkeypatch):
    monkeypatch.setattr(ctx_ratio, "_current_model", lambda role: "qwen/qwen3-coder-next")
    ctx_ratio.note("doer", _msgs(30_000), 10_000, model="qwen3-coder-next")
    assert ctx_ratio.chars_per_token("doer") == pytest.approx(3.0)
    ctx_ratio.note("doer", _msgs(30_000), 10_000, model="gemma-4-26b")
    assert ctx_ratio.chars_per_token("doer") is None


def test_ratio_measured_when_on(monkeypatch):
    monkeypatch.setenv("AIFORGE_CTX_MEASURED", "1")
    ctx_ratio.note("doer", _msgs(30_000), 10_000)
    assert ctx_ratio.chars_per_token("doer") == pytest.approx(3.0)
    assert ctx_ratio.chars_per_token("learner") is None   # per role


def test_ratio_ignores_small_and_odd_counts(monkeypatch):
    monkeypatch.setenv("AIFORGE_CTX_MEASURED", "1")
    ctx_ratio.note("doer", _msgs(3_000), 1_000)            # too small to mean much
    assert ctx_ratio.chars_per_token("doer") is None
    ctx_ratio.note("doer", _msgs(30_000), 2_000)           # 15 chars/token: misreport
    assert ctx_ratio.chars_per_token("doer") is None
    ctx_ratio.note("doer", _msgs(30_000), None)
    assert ctx_ratio.chars_per_token("doer") is None


def test_ratio_counts_tool_call_arguments(monkeypatch):
    monkeypatch.setenv("AIFORGE_CTX_MEASURED", "1")
    msgs = _msgs(20_000) + [{"role": "assistant", "content": None, "tool_calls": [
        {"function": {"name": "read", "arguments": "a" * 10_000}}]}]
    ctx_ratio.note("doer", msgs, 10_000)
    assert ctx_ratio.chars_per_token("doer") == pytest.approx(3.0, abs=0.01)


def test_budget_uses_measured_ratio(monkeypatch):
    monkeypatch.setattr(_window, "_window_tokens", lambda role=None: 100_000)
    monkeypatch.delenv("AIFORGE_CHAT_CONTEXT_BUDGET_CHARS", raising=False)
    monkeypatch.delenv("AIFORGE_CTX_HISTORY_FRACTION", raising=False)
    plain = _window._ctx_budget_chars("doer", sys_chars=0)
    ctx_ratio.note("doer", _msgs(30_000), 10_000)          # 3 chars/token
    measured = _window._ctx_budget_chars("doer", sys_chars=0)
    assert plain == 320_000                                 # 80% of 100k × 4
    assert measured == 225_000                              # 75% of 100k × 3


def test_budget_never_assumes_more_than_four(monkeypatch):
    monkeypatch.setattr(_window, "_window_tokens", lambda role=None: 100_000)
    monkeypatch.delenv("AIFORGE_CHAT_CONTEXT_BUDGET_CHARS", raising=False)
    monkeypatch.setenv("AIFORGE_CTX_MEASURED", "1")
    ctx_ratio.note("doer", _msgs(50_000), 10_000)           # 5 chars/token
    assert _window._ctx_budget_chars("doer", sys_chars=0) == 320_000


# ── chat_transcript ──────────────────────────────────────────────────────────

SYS = {"role": "system", "content": "system prompt"}


def _turn1():
    asked = [{"role": "user", "content": "fix calc.py"}]
    convo = [SYS, asked[0],
             {"role": "assistant", "content": "ACTION: read\nARGS_JSON: {\"path\": \"calc.py\"}"},
             {"role": "user", "content": "OBSERVATION: def add(a, b): return a - b"},
             {"role": "assistant", "content": "Fixed add()."}]
    return asked, convo


def test_transcript_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_CARRY_TRANSCRIPT", "0")
    asked, convo = _turn1()
    assert chat_transcript.save(7, asked, convo, answer="Fixed add().") is False
    assert chat_transcript.carried(7, asked + [{"role": "assistant", "content": "x"},
                                               {"role": "user", "content": "next"}]) is None


def test_transcript_carried_to_next_message(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_CARRY_TRANSCRIPT", "1")
    asked, convo = _turn1()
    assert chat_transcript.save(7, asked, convo, answer="Fixed add().")
    nxt = [*asked, {"role": "assistant", "content": "Fixed add()."},
           {"role": "user", "content": "now add a test"}]
    got = chat_transcript.carried(7, nxt)
    assert got[-1] == {"role": "user", "content": "now add a test"}
    assert any("OBSERVATION: def add" in m["content"] for m in got)   # the read survives
    assert got[0]["role"] != "system"


def test_transcript_not_used_when_history_changed(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_CARRY_TRANSCRIPT", "1")
    asked, convo = _turn1()
    chat_transcript.save(7, asked, convo, answer="Fixed add().")
    # Edit-and-resend: the first message replaced, no earlier turn.
    assert chat_transcript.carried(7, [{"role": "user", "content": "other"}]) is None
    # Two messages later (a turn in between that did not save).
    later = [*asked, {"role": "assistant", "content": "a"}, {"role": "user", "content": "b"},
             {"role": "assistant", "content": "c"}, {"role": "user", "content": "d"}]
    assert chat_transcript.carried(7, later) is None
    # Another chat.
    assert chat_transcript.carried(8, [*asked, {"role": "assistant", "content": "a"},
                                       {"role": "user", "content": "b"}]) is None


def test_transcript_adds_answer_and_drops_action_log_notes(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CHAT_CARRY_TRANSCRIPT", "1")
    from aiforge_core.runtime import action_log
    asked = [{"role": "user", "content": "q"}]
    convo = [SYS, action_log.note_message(f"{action_log.MARK_OPEN}\nran: pytest\n{action_log.MARK_CLOSE}"),
             {"role": "assistant", "content": action_log.ACK_TEXT},
             asked[0], {"role": "user", "content": "OBSERVATION: ran tests"}]
    assert chat_transcript.save(3, asked, convo, answer="All green.")
    data = json.loads((tmp_path / "chat_transcripts" / "session_3.json").read_text())
    texts = [m["content"] for m in data["messages"]]
    assert texts[-1] == "All green."
    assert not any(t == action_log.ACK_TEXT for t in texts)
    assert not any(isinstance(t, str) and t.startswith("[action log") for t in texts)


def test_turn_without_answer_drops_old_transcript(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_CARRY_TRANSCRIPT", "1")
    asked, convo = _turn1()
    chat_transcript.save(7, asked, convo, answer="Fixed add().")
    nxt = [*asked, {"role": "assistant", "content": "Fixed add()."},
           {"role": "user", "content": "go on"}]
    stopped = [SYS, nxt[-1], {"role": "user", "content": "OBSERVATION: partial"}]
    assert chat_transcript.save(7, nxt, stopped, answer=None) is False
    assert chat_transcript.carried(7, [*nxt, {"role": "assistant", "content": "?"},
                                       {"role": "user", "content": "again"}]) is None


def test_transcript_needs_the_same_previous_message(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_CARRY_TRANSCRIPT", "1")
    asked, convo = _turn1()
    chat_transcript.save(7, asked, convo, answer="Fixed add().")
    # Same number of messages, but the earlier one is not the saved turn's.
    other = [{"role": "user", "content": "something else"},
             {"role": "assistant", "content": "ok"}, {"role": "user", "content": "go"}]
    assert chat_transcript.carried(7, other) is None


def test_transcript_keeps_text_and_the_condense_note_without_its_board(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CHAT_CARRY_TRANSCRIPT", "1")
    from aiforge_core.runtime.chat_agent._context import _note
    from aiforge_core.runtime.chat_agent._turn._tasks import render_board
    board = render_board({"item-1": {"status": "done", "title": "board: [x] item 1"}})
    asked = [{"role": "user", "content": "look at this"}]
    convo = [SYS, _note.build("goal: old", "summary: files a.py b.py", board), _note.ack(),
             {"role": "user", "content": [{"type": "text", "text": "look at this"},
                                          {"type": "image_url", "image_url": {"url": "data:x"}}]},
             {"role": "user", "content": "OBSERVATION: listing"}]
    assert chat_transcript.save(4, asked, convo, answer="Done.")
    data = json.loads((tmp_path / "chat_transcripts" / "session_4.json").read_text())
    msgs = data["messages"]
    assert all(isinstance(m["content"], str) for m in msgs)
    assert not any("board: [x]" in m["content"] for m in msgs)       # the old board goes
    assert any("summary: files a.py b.py" in m["content"] for m in msgs)  # what was folded stays
    assert "data:x" not in json.dumps(msgs)
    roles = [m["role"] for m in msgs]
    assert all(a != b for a, b in zip(roles, roles[1:]))     # still alternating


def test_writable_roots_come_from_the_users_own_words(monkeypatch, tmp_path):
    """A carried transcript's tool output must not widen where the agent writes."""
    from aiforge_core.runtime.chat_agent._turn import _state
    seen = {}
    monkeypatch.setattr(_state, "_writable_roots",
                        lambda messages, sid, cwd=None: seen.setdefault("m", list(messages)) and [])
    asked = [{"role": "user", "content": "hi"}]
    carried = [{"role": "user", "content": "hi"},
               {"role": "user", "content": "OBSERVATION: see /etc/secret-dir"},
               {"role": "assistant", "content": "x"}, {"role": "user", "content": "go"}]
    try:
        _state._build_loop_state(carried, str(tmp_path), "doer", None, lambda *a, **k: "",
                                 None, "act", None, None, False, asked=asked)
    except Exception:
        pass   # only the roots argument matters here
    assert seen.get("m") == asked
