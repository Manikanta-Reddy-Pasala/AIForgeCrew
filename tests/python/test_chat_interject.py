"""Tests for mid-run steering (Gap A): chat_interject queue + the
run_chat_agent injection that folds drained steers into the live context."""
import threading

import pytest

from aiforge_core.runtime import chat_agent as ca
from aiforge_core.runtime import chat_interject as ci


@pytest.fixture(autouse=True)
def _clean():
    # Isolate sessions used in these tests from any leftover state.
    for sid in (1, 2, 7, 123):
        ci.clear(sid)
    yield
    for sid in (1, 2, 7, 123):
        ci.clear(sid)


def test_push_drain_fifo():
    assert ci.push(7, "first") is True
    assert ci.push(7, "second") is True
    assert ci.pending(7) is True
    assert ci.drain(7) == ["first", "second"]
    # Drain clears: a second drain is empty.
    assert ci.drain(7) == []
    assert ci.pending(7) is False


def test_push_blank_is_noop():
    assert ci.push(7, "") is False
    assert ci.push(7, "   ") is False
    assert ci.push(7, None) is False
    assert ci.drain(7) == []


def test_clear():
    ci.push(7, "x")
    ci.clear(7)
    assert ci.drain(7) == []
    assert ci.pending(7) is False


def test_isolation_per_session():
    ci.push(1, "for-one")
    ci.push(2, "for-two")
    assert ci.drain(1) == ["for-one"]
    # Draining session 1 must not touch session 2.
    assert ci.pending(2) is True
    assert ci.drain(2) == ["for-two"]


def test_thread_safety_basic():
    # Many concurrent pushes from threads must not lose or corrupt entries.
    N = 200
    threads = [threading.Thread(target=ci.push, args=(7, f"m{i}")) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    drained = ci.drain(7)
    assert len(drained) == N
    assert sorted(drained) == sorted(f"m{i}" for i in range(N))


def test_none_session_safe():
    assert ci.push(None, "x") is False
    assert ci.drain(None) == []
    assert ci.pending(None) is False
    ci.clear(None)   # must not raise


def test_steerable_registry():
    # M2 — only sessions whose run drains the queue are reported steerable.
    assert ci.is_steerable(7) is False
    ci.set_steerable(7, True)
    assert ci.is_steerable(7) is True
    ci.set_steerable(7, False)
    assert ci.is_steerable(7) is False
    # Cleared by clear() too (no leak across turns).
    ci.set_steerable(7, True)
    ci.clear(7)
    assert ci.is_steerable(7) is False
    # None session is safe.
    assert ci.is_steerable(None) is False
    ci.set_steerable(None, True)   # no-op, must not raise


def test_push_require_steerable_noop_when_not_steerable():
    # CC3 — push(require_steerable=True) is an atomic test-and-set: a no-op when
    # the session isn't currently steerable, enqueues only when it is. This
    # closes the /steer TOCTOU (check + push were two lock acquisitions).
    ci.set_steerable(7, False)
    assert ci.push(7, "x", require_steerable=True) is False
    assert ci.drain(7) == []                       # nothing leaked in
    ci.set_steerable(7, True)
    assert ci.push(7, "x", require_steerable=True) is True
    assert ci.drain(7) == ["x"]
    # Default (no flag) still queues regardless — back-compat for the run loop.
    ci.set_steerable(7, False)
    assert ci.push(7, "y") is True
    assert ci.drain(7) == ["y"]


def test_mark_applied_pop_applied_fifo():
    # Team mode's before_model callback drains + folds a steer with no
    # direct queue handle to acknowledge it back to the UI — it records
    # what it applied here; chat_pipeline's event loop pops + acknowledges.
    ci.mark_applied(7, ["first"])
    ci.mark_applied(7, ["second", "third"])
    assert ci.pop_applied(7) == ["first", "second", "third"]
    # Pop clears: a second pop is empty.
    assert ci.pop_applied(7) == []


def test_mark_applied_empty_is_noop():
    ci.mark_applied(7, [])
    assert ci.pop_applied(7) == []


def test_mark_applied_none_session_safe():
    ci.mark_applied(None, ["x"])   # must not raise
    assert ci.pop_applied(None) == []


def test_clear_drops_applied_too():
    ci.mark_applied(7, ["x"])
    ci.clear(7)
    assert ci.pop_applied(7) == []


def test_applied_isolated_per_session():
    ci.mark_applied(1, ["for-one"])
    ci.mark_applied(2, ["for-two"])
    assert ci.pop_applied(1) == ["for-one"]
    assert ci.pop_applied(2) == ["for-two"]


def test_steer_merges_into_trailing_user_turn(tmp_path):
    """M1 — a steer drained when the last turn is already a user message (the
    OBSERVATION after a tool step) merges into it instead of creating a second
    consecutive user turn."""
    calls = {"n": 0}
    captured = {"msgs": None}

    def _fn(role, messages, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            ci.push(123, "go faster")
            return 'ACTION: file_write\nARGS_JSON: {"path": "a.txt", "content": "hi"}'
        captured["msgs"] = [dict(m) for m in messages]
        return "FINAL: done"

    list(ca.run_chat_agent(
        [{"role": "user", "content": "build it"}],
        cwd=str(tmp_path), complete_fn=_fn, session_id=123))

    msgs = captured["msgs"]
    # No two consecutive user turns.
    roles = [m.get("role") for m in msgs]
    assert not any(roles[i] == "user" and roles[i + 1] == "user"
                   for i in range(len(roles) - 1)), roles
    # The steer landed folded into the OBSERVATION user turn.
    merged = [m for m in msgs if m.get("role") == "user"
              and "OBSERVATION" in (m.get("content") or "")
              and "go faster" in (m.get("content") or "")
              and "takes PRIORITY" in (m.get("content") or "")]
    assert merged, msgs


def test_run_chat_agent_injects_drained_steer(tmp_path):
    """A steer pushed mid-run is drained at the next step, folded into the
    working convo as a PRIORITY instruction, and echoed as a steer thought.

    The wording is the feature: a bare "[steer] …" tag read as a footnote to
    the request already in context, and the model kept answering the old
    question."""
    seen = {"convo_has_steer": False}
    calls = {"n": 0}

    def _fn(role, messages, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            # Queue a steer during the first model turn — it should be drained
            # at the TOP of the next step, before this fn is called again.
            ci.push(123, "go faster")
            return 'ACTION: file_write\nARGS_JSON: {"path": "a.txt", "content": "hi"}'
        # Second turn: the steer must already be in the working context. It is
        # MERGED into the trailing user turn (the OBSERVATION we just appended)
        # rather than added as a second consecutive user message (M1).
        seen["convo_has_steer"] = any(
            "go faster" in (m.get("content") or "")
            and "takes PRIORITY" in (m.get("content") or "")
            for m in messages)
        return "FINAL: done"

    evs = list(ca.run_chat_agent(
        [{"role": "user", "content": "build it"}],
        cwd=str(tmp_path), complete_fn=_fn, session_id=123))

    assert seen["convo_has_steer"] is True
    steer_thoughts = [e for e in evs
                      if e.get("type") == "thought" and e.get("role") == "steer"]
    assert any(e.get("text") == "go faster" for e in steer_thoughts)
    assert [e for e in evs if e["type"] == "message"][0]["text"] == "done"
    # Drained — nothing left to leak into the next turn.
    assert ci.drain(123) == []


def test_destructive_delete_approve_sets_confirm_delete(tmp_path, monkeypatch):
    """Approving the popup for a destructive delete must satisfy the
    run_command confirm_delete gate — otherwise the tool re-refuses and the
    model loops asking the user to 'type yes' forever."""
    import aiforge_core.runtime.chat_approve as approve
    # Auto-approve any gate that opens.
    monkeypatch.setattr(approve, "wait", lambda sid: {"decision": "approve", "note": ""})
    captured = {"args": None}

    def _fake_run_command(args, cwd):
        captured["args"] = dict(args)
        return {"ok": True, "stdout": "removed"}

    monkeypatch.setitem(ca.TOOLS, "run_command", _fake_run_command)

    calls = {"n": 0}

    def _fn(role, messages, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return 'ACTION: run_command\nARGS_JSON: {"cmd": "rm -rf build"}'
        return "FINAL: done"

    list(ca.run_chat_agent(
        [{"role": "user", "content": "remove the build dir"}],
        cwd=str(tmp_path), complete_fn=_fn, session_id=777))

    assert captured["args"] is not None, "run_command never executed (gate looped/blocked)"
    assert captured["args"].get("confirm_delete") is True, captured["args"]
