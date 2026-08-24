"""Context-retention helpers: the step digest fed into history, the history
builder (keep stepful turns + merge same-role), and the prompt compressor."""
from aiforge_core.runtime import chat_agent as ca


def test_step_digest_summarises_tools():
    from aiforge_core.api.api import _step_digest
    steps = [
        {"type": "thought", "text": "thinking"},
        {"type": "tool", "name": "file_read", "args": {"path": "a/b.py"},
         "result": {"ok": True}},
        {"type": "tool", "name": "run_command", "args": {"cmd": "pytest"},
         "result": {"ok": False, "error": "1 failed"}},
    ]
    d = _step_digest(steps)
    assert "file_read(a/b.py)✓" in d
    assert "run_command(pytest)✗" in d


def test_history_folds_digest_and_keeps_stepful_blank_turn():
    from aiforge_core.api.api import _chat_history_for_agent
    rows = [
        {"role": "user", "content": "build X"},
        # did work but produced no final text — must NOT be dropped.
        {"role": "assistant", "content": "",
         "steps": [{"type": "tool", "name": "editor", "args": {"path": "x.py"},
                    "result": {"ok": True}}]},
        {"role": "user", "content": "now test it"},
    ]
    h = _chat_history_for_agent(rows)
    # The stepful blank assistant turn survives as a digest line.
    assert any(m["role"] == "assistant" and "did:" in m["content"] for m in h)
    assert "editor(x.py)" in h[1]["content"]
    # No two consecutive same-role turns.
    for a, b in zip(h, h[1:]):
        assert a["role"] != b["role"]


def test_history_merges_consecutive_same_role():
    from aiforge_core.api.api import _chat_history_for_agent
    rows = [
        {"role": "user", "content": "one"},
        {"role": "user", "content": "two"},   # consecutive user → merged
    ]
    h = _chat_history_for_agent(rows)
    assert len(h) == 1
    assert h[0]["role"] == "user"
    assert "one" in h[0]["content"]
    assert "two" in h[0]["content"]


def test_compress_prompt_collapses_blanks_and_dupes(monkeypatch):
    monkeypatch.delenv("AIFORGE_CHAT_COMPRESS_PROMPT", raising=False)
    text = "line a\n\n\n\nline a\nline b   \n\n\n"
    out = ca._compress_prompt(text)
    # No 2+ consecutive blank lines, trailing spaces stripped, dup line dropped.
    assert "\n\n\n" not in out
    assert "line a\nline b" in out or "line a\n\nline b" in out
    assert "   \n" not in out


def test_compress_prompt_disabled(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_COMPRESS_PROMPT", "0")
    text = "a\n\n\n\nb"
    assert ca._compress_prompt(text) == text


def test_condense_summary_rolls_forward_across_two_condenses(monkeypatch):
    """L-1: a 2nd condense keeps the FIRST window's asks (carried out of the
    prior breadcrumb), not just the latest window's."""
    monkeypatch.setenv("AIFORGE_CHAT_CONTEXT_BUDGET_CHARS", "300")
    # First condense: middle contains the original ask.
    convo = [{"role": "system", "content": "SYS"},
             {"role": "user", "content": "build the UNIQUEALPHA exporter"}]
    for i in range(20):
        convo.append({"role": "assistant", "content": "ACTION: grep\nq"})
        convo.append({"role": "user", "content": "OBSERVATION: " + "x" * 40})
    out1 = ca._compact_convo(convo, keep_recent=4)
    assert "UNIQUEALPHA" in out1[0]["content"]
    # Continue the session with NEW turns, then condense again.
    for i in range(20):
        out1.append({"role": "assistant", "content": "ACTION: grep\nq"})
        out1.append({"role": "user", "content": "OBSERVATION: " + "y" * 40})
    out2 = ca._compact_convo(out1, keep_recent=4)
    # The original ask survived the second condense (rolled forward).
    assert "UNIQUEALPHA" in out2[0]["content"]
