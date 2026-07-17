"""Bug1 — compaction must NOT fold an un-backed edit claim into the rolling
'Earlier outcomes' breadcrumb (that made the model believe a hallucinated edit
had happened, forever after)."""
from __future__ import annotations

from aiforge_core.runtime.chat_agent._context._compaction import _compact_convo


def _long_convo():
    """A convo long enough to force a condense: a real decision outcome, a
    hallucinated edit claim, all buried in a big middle."""
    convo = [{"role": "system", "content": "SYS"}]
    # bulky middle to blow past any context budget
    for i in range(30):
        convo.append({"role": "user", "content": f"ask {i} " + "x" * 3000})
        convo.append({"role": "assistant", "content": f"OBSERVATION filler {i} " + "y" * 3000})
    # a genuine decision (should survive) and a hallucinated claim (should NOT)
    convo.append({"role": "assistant",
                  "content": "Decided to use a WorkQueue stream for retries."})
    convo.append({"role": "assistant",
                  "content": "I have now applied the fix to src/views/Dashboard.vue."})
    # recent tail
    for i in range(4):
        convo.append({"role": "user", "content": f"recent {i}"})
        convo.append({"role": "assistant", "content": f"reply {i}"})
    return convo


def test_edit_claim_not_persisted_as_outcome(monkeypatch):
    # Force a small history budget so the condense actually fires regardless of
    # the resolved model window.
    import aiforge_core.runtime.chat_agent._context._compaction as comp
    monkeypatch.setattr(comp, "_ctx_budget_chars", lambda *a, **k: 20000)
    out = _compact_convo(_long_convo(), keep_recent=4)
    sys_text = out[0]["content"]
    # It actually condensed (breadcrumb present).
    assert "auto-condensed" in sys_text
    # The hallucinated edit claim is NOT carried forward as an outcome…
    assert "applied the fix to src/views/Dashboard.vue" not in sys_text
    # …while a genuine decision still can be.
    assert "WorkQueue" in sys_text
