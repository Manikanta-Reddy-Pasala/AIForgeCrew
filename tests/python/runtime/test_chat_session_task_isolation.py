"""A new chat must not inherit the previous chat's TASK.

Knowledge crossing sessions is wanted (memory recall, the memory_lookup /
search_chat_sessions tools, and same-project continuity). Work is not: a
session opened to ask about one thing was continuing an unrelated session's
job — editing a repo the user never mentioned in it — because the opening turn
injected the previous session's transcript tail verbatim, whatever project it
belonged to.

These pin the two halves of the fix in ``_append_recall_blocks``:
  * the previous-session block is built with the CURRENT session's cwd, so a
    different working tree's session no longer qualifies; and
  * the recall exclusion follows what was actually injected — when nothing was,
    the prior session's recall hits must still surface (they are the knowledge
    half, and they carry the no-resume framing).
"""
import types

from aiforge_core.runtime.chat_agent import _loop


def _bundle():
    return types.SimpleNamespace(memory_md="MEM")


def _capture(monkeypatch):
    """Record what _append_recall_blocks emits + what the recall helper saw."""
    blocks: list = []
    seen: dict = {}

    def _fake_learning(add, bundle, last_user, session_id, proactive,
                       is_init, prev_session_on, cwd=None):
        seen["prev_session_on"] = prev_session_on
        seen["cwd"] = cwd

    monkeypatch.setattr(_loop, "_append_learning_recall", _fake_learning)
    monkeypatch.setattr(_loop, "_append_session_blocks",
                        lambda *a, **k: [])
    return blocks, seen


def test_other_project_session_is_not_carried(monkeypatch):
    blocks, seen = _capture(monkeypatch)
    calls = {}

    def _brief(session_id, *, cwd=None, **k):
        calls["cwd"] = cwd
        return ""            # different project → nothing qualifies

    monkeypatch.setattr(
        "aiforge_core.runtime.chat_okr.previous_session_brief", _brief)
    _loop._append_recall_blocks(
        lambda tag, text: blocks.append((tag, text)), _bundle(),
        "/repo/session-73", "how do I curl the model?", [], 73, "chat",
        "lite", True)

    # The current session's cwd decides what counts as "the previous session".
    assert calls["cwd"] == "/repo/session-73"
    assert not any(tag == "prev-session" for tag, _ in blocks)
    # Nothing was injected, so nothing may be excluded from recall.
    assert seen["prev_session_on"] is False
    assert seen["cwd"] == "/repo/session-73"


def test_same_project_session_is_carried_and_deduped(monkeypatch):
    blocks, seen = _capture(monkeypatch)
    monkeypatch.setattr(
        "aiforge_core.runtime.chat_okr.previous_session_brief",
        lambda session_id, *, cwd=None, **k: "PREVIOUS SESSION 8 — REFERENCE ONLY")
    _loop._append_recall_blocks(
        lambda tag, text: blocks.append((tag, text)), _bundle(),
        "/repo", "carry on", [], 9, "chat", "lite", True)

    assert ("prev-session", "PREVIOUS SESSION 8 — REFERENCE ONLY") in blocks
    assert seen["prev_session_on"] is True


def test_disabled_by_env(monkeypatch):
    blocks, seen = _capture(monkeypatch)
    monkeypatch.setenv("AIFORGE_SESSION_PREV_CONTEXT", "0")

    def _never(*a, **k):                       # must not even be consulted
        raise AssertionError("previous_session_brief called while disabled")

    monkeypatch.setattr(
        "aiforge_core.runtime.chat_okr.previous_session_brief", _never)
    _loop._append_recall_blocks(
        lambda tag, text: blocks.append((tag, text)), _bundle(),
        "/repo", "hi", [], 9, "chat", "lite", True)
    assert not any(tag == "prev-session" for tag, _ in blocks)
    assert seen["prev_session_on"] is False


def test_not_carried_on_follow_up_turns(monkeypatch):
    """Opening turn only — a mid-session turn never re-injects it."""
    blocks, seen = _capture(monkeypatch)

    def _never(*a, **k):
        raise AssertionError("previous_session_brief called mid-session")

    monkeypatch.setattr(
        "aiforge_core.runtime.chat_okr.previous_session_brief", _never)
    _loop._append_recall_blocks(
        lambda tag, text: blocks.append((tag, text)), _bundle(),
        "/repo", "and then?", [], 9, "chat", "lite", False)
    assert not any(tag == "prev-session" for tag, _ in blocks)
