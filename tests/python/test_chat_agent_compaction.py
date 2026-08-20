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
    # Size-aware tail: the kept recent slice FITS the (tiny 2000-char) budget so
    # condense actually frees the window, with a usable floor (>=4). Bounded by
    # chars, not a fixed count.
    tail = out[1:]
    assert 4 <= len(tail) < 60
    assert sum(len(m.get("content") or "") for m in tail) <= 2000
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


def test_cave_mode_keeps_quality_blocks_and_shrinks_budget(tmp_path, monkeypatch):
    """Cave mode condenses HISTORY sooner (smaller budget) but does NOT drop
    quality context: skills, mentions AND workflows are all still assembled —
    they're static how-to context, not the growing history that makes small
    models drift. Dropping skills to save tokens was a quality regression."""
    from aiforge_core.runtime import chat_agent as ca
    seen = {"skills": 0, "workflows": 0, "mentions": 0}
    import aiforge_core.runtime.skills as sk
    import aiforge_core.runtime.workflows as wf
    import aiforge_core.runtime.mentions as mn
    monkeypatch.setattr(sk, "auto_context", lambda *a, **k: (seen.__setitem__("skills", seen["skills"] + 1), "SKILLS")[1])
    monkeypatch.setattr(wf, "auto_context", lambda *a, **k: (seen.__setitem__("workflows", seen["workflows"] + 1), "WF")[1])
    monkeypatch.setattr(mn, "expand", lambda *a, **k: (seen.__setitem__("mentions", seen["mentions"] + 1), ("M", 0))[1])

    # Budget shrinks in cave mode (condense fires sooner).
    monkeypatch.setenv("AIFORGE_CAVE_MODE", "0")
    monkeypatch.delenv("AIFORGE_CHAT_CONTEXT_BUDGET_CHARS", raising=False)
    normal = ca._ctx_budget_chars()
    monkeypatch.setenv("AIFORGE_CAVE_MODE", "1")
    assert ca._ctx_budget_chars() < normal

    fn = _scripted(["FINAL: done"])
    list(ca.run_chat_agent([{"role": "user", "content": "hi"}],
                           cwd=str(tmp_path), complete_fn=fn))
    # Cave keeps the QUALITY blocks — nothing dropped to save tokens.
    assert seen["skills"] >= 1 and seen["mentions"] >= 1
    assert seen["workflows"] >= 1


def test_condense_tail_capped_by_size_not_count(monkeypatch):
    """Size-aware tail: with LARGE recent turns the verbatim tail is capped by
    CHARS (~half the budget), not a fixed count — so condense always frees the
    window instead of keeping N huge tool-outputs. Small turns keep more."""
    from aiforge_core.runtime.chat_agent._context._compaction import (
        _compact_convo, _recent_tail_count)
    monkeypatch.setenv("AIFORGE_CHAT_CONTEXT_BUDGET_CHARS", "20000")
    budget = 20000

    def _run(turn_chars):
        convo = [{"role": "system", "content": "S" * 500}]
        for i in range(40):
            convo.append({"role": "assistant", "content": "ACTION: file_read\nARGS_JSON: {}"})
            convo.append({"role": "user", "content": "OBSERVATION: " + "x" * turn_chars})
        out = _compact_convo(convo, keep_recent=18)
        tail = out[1:]
        return len(tail), sum(len(m.get("content") or "") for m in tail)

    big_n, big_chars = _run(4000)     # huge turns
    small_n, small_chars = _run(100)  # tiny turns
    # both tails fit ~half the budget (freed the window)
    assert big_chars <= budget and small_chars <= budget
    # big turns → FEWER kept (size cap bites); small turns → more (up to ceiling)
    assert big_n < small_n
    assert big_n >= 4                 # never below the floor

    # helper: floor honoured even when the first message already exceeds the cap
    huge = [{"role": "system", "content": ""}] + [
        {"role": "user", "content": "z" * 99999} for _ in range(10)]
    assert _recent_tail_count(huge, budget=1000, ceiling=18, floor=4) == 4


def test_force_condenses_a_history_that_still_fits(monkeypatch):
    """The runaway-cap extension wants a FRESH window, not merely a safe one:
    ``force=True`` folds the middle even though the history is under budget."""
    from aiforge_core.runtime import chat_agent as ca
    monkeypatch.setenv("AIFORGE_CHAT_CONTEXT_BUDGET_CHARS", "100000")
    convo = [{"role": "system", "content": "S" * 100}]
    for _i in range(30):
        convo.append({"role": "assistant",
                      "content": "THOUGHT: t\nACTION: file_read\nARGS_JSON: {}"})
        convo.append({"role": "user", "content": "OBSERVATION: " + "x" * 50})
    assert ca._compact_convo(list(convo), keep_recent=8) == convo   # fits → untouched
    out = ca._compact_convo(list(convo), keep_recent=8, force=True)
    assert len(out) < len(convo)
    assert "auto-condensed" in out[0]["content"]
    assert out[0]["role"] == "system" and out[-1]["role"] == "user"
