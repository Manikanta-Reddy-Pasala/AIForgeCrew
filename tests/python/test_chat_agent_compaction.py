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


def test_compact_convo_condenses_long_history(monkeypatch):
    from aiforge_core.runtime import chat_agent as ca
    monkeypatch.setenv("AIFORGE_CHAT_CONTEXT_BUDGET_CHARS", "2000")
    convo = [{"role": "system", "content": "S" * 100}]
    for i in range(30):
        convo.append({"role": "assistant",
                      "content": "THOUGHT: t\nACTION: file_read\nARGS_JSON: {}"})
        convo.append({"role": "user", "content": "OBSERVATION: " + "x" * 200})
    out = ca._compact_convo(convo, keep_recent=8)
    assert out[0]["role"] == "system"                      # system preserved
    # breadcrumb folded INTO the system message (no separate user turn → no
    # consecutive same-role messages); actions summarized.
    assert "auto-condensed" in out[0]["content"]
    assert "file_read" in out[0]["content"]
    # L-3: keep_recent is scaled DOWN to the (tiny 2000-char) budget — 8 turns
    # of 200+ chars each wouldn't fit — so the tail is the adaptive 4, not 8.
    assert len(out) == 1 + 4                               # system + adaptive tail
    # no two consecutive non-system same-role messages
    roles = [m["role"] for m in out]
    assert not any(roles[i] == roles[i+1] != "system" for i in range(len(roles)-1))
    assert roles[-1] == "user"                             # model continues
    assert sum(len(m["content"]) for m in out) < \
        sum(len(m["content"]) for m in convo)


def test_compact_convo_noop_under_budget(monkeypatch):
    from aiforge_core.runtime import chat_agent as ca
    monkeypatch.setenv("AIFORGE_CHAT_CONTEXT_BUDGET_CHARS", "48000")
    convo = [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}]
    assert ca._compact_convo(convo) is convo               # untouched


def test_compact_convo_sentinel_strip_is_exact(monkeypatch):
    # A condense block is stripped by unique sentinel, so a legit look-alike
    # phrase elsewhere in the system message is never eaten, and the block
    # can't accumulate across repeated condenses.
    from aiforge_core.runtime import chat_agent as ca
    monkeypatch.setenv("AIFORGE_CHAT_CONTEXT_BUDGET_CHARS", "800")
    sysc = ("KEEP_ME. mentions '[earlier conversation auto-condensed ... "
            "this point.]' literally. KEEP_END.")
    convo = [{"role": "system", "content": sysc}]
    for i in range(20):
        convo.append({"role": "assistant", "content": "ACTION: grep\nARGS_JSON: {}"})
        convo.append({"role": "user", "content": "OBS " + "z" * 100})
    out = ca._compact_convo(convo)
    out = ca._compact_convo(out)          # re-condense
    out = ca._compact_convo(out)
    s = out[0]["content"]
    assert "KEEP_ME" in s and "KEEP_END" in s          # legit text preserved
    assert s.count(ca._CONDENSE_OPEN) == 1             # exactly one block


def test_usage_event_emitted(tmp_path):
    fn = _scripted(["THOUGHT: x\nFINAL: done"])
    evs = _collect(ca.run_chat_agent(
        [{"role": "user", "content": "hi"}], cwd=str(tmp_path), complete_fn=fn))
    usage = [e for e in evs if e["type"] == "usage"]
    assert usage and 0 <= usage[0]["pct"] <= 100
    assert usage[0]["budget_chars"] > 0


def test_usage_meter_counts_history_not_system_prompt(tmp_path):
    """Meter regression: the usage event must mirror _compact_convo's math —
    HISTORY-ONLY chars (the system prompt is reserved out of the budget, not
    re-counted). The old version summed the whole convo incl. the tens-of-KB
    system prompt, so the meter jumped between turns as recall blocks changed."""
    fn = _scripted(["THOUGHT: x\nFINAL: done"])
    evs = _collect(ca.run_chat_agent(
        [{"role": "user", "content": "hi"}], cwd=str(tmp_path), complete_fn=fn))
    usage = [e for e in evs if e["type"] == "usage"]
    assert usage and usage[0]["context_chars"] < 5000


def test_condense_summary_includes_earlier_asks(monkeypatch):
    """A condensed middle carries earlier asks/outcomes, not just tool counts."""
    monkeypatch.setenv("AIFORGE_CHAT_CONTEXT_BUDGET_CHARS", "200")
    convo = [{"role": "system", "content": "SYS"}]
    convo.append({"role": "user", "content": "build the invoice exporter please"})
    convo.append({"role": "assistant", "content": "ACTION: file_write\nfoo"})
    for i in range(30):
        convo.append({"role": "user", "content": f"OBSERVATION: {'x' * 50}"})
        convo.append({"role": "assistant", "content": f"ACTION: grep\nq{i}"})
    out = ca._compact_convo(convo, keep_recent=4)
    sys_text = out[0]["content"]
    assert "auto-condensed" in sys_text
    assert "Earlier asks:" in sys_text and "invoice exporter" in sys_text


def test_cave_mode_skips_optional_blocks_and_shrinks_budget(tmp_path, monkeypatch):
    """Cave mode drops skills/mentions blocks + condenses sooner. WORKFLOWS
    are still built even in cave — a matched workflow is a MANDATORY user
    procedure (branch/MR conventions); silently dropping it on small windows
    made the agent e.g. commit straight to main."""
    from aiforge_core.runtime import chat_agent as ca
    seen = {"skills": 0, "workflows": 0, "mentions": 0}
    import aiforge_core.runtime.skills as sk
    import aiforge_core.runtime.workflows as wf
    import aiforge_core.runtime.mentions as mn
    monkeypatch.setattr(sk, "auto_context", lambda *a, **k: (seen.__setitem__("skills", seen["skills"] + 1), "SKILLS")[1])
    monkeypatch.setattr(wf, "auto_context", lambda *a, **k: (seen.__setitem__("workflows", seen["workflows"] + 1), "WF")[1])
    monkeypatch.setattr(mn, "expand", lambda *a, **k: (seen.__setitem__("mentions", seen["mentions"] + 1), ("M", 0))[1])

    # Budget shrinks in cave mode.
    monkeypatch.setenv("AIFORGE_CAVE_MODE", "0")
    monkeypatch.delenv("AIFORGE_CHAT_CONTEXT_BUDGET_CHARS", raising=False)
    normal = ca._ctx_budget_chars()
    monkeypatch.setenv("AIFORGE_CAVE_MODE", "1")
    assert ca._ctx_budget_chars() < normal

    fn = _scripted(["FINAL: done"])
    list(ca.run_chat_agent([{"role": "user", "content": "hi"}],
                           cwd=str(tmp_path), complete_fn=fn))
    # In cave mode the OPTIONAL blocks were never assembled; workflows
    # (mandatory procedures) still were.
    assert seen["skills"] == 0 and seen["mentions"] == 0
    assert seen["workflows"] >= 1
