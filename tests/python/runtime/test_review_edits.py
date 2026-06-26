"""Gap D — pre-apply diff-review mode.

Covers:
  (a) the per-session review-edits flag in chat_approve (set/get/clear +
      isolation between sessions);
  (b) the ADK Doer's tool gate forcing approval for a mutating tool when the
      flag is on and an emitter is attached, reject short-circuits, and the
      flag-off / no-emitter cases fall through to allow;
  (c) the shared unified_preview helper producing a diff for an existing file
      and handling a new file.

Hermetic: the approval emitter + waiter are driven from a side thread, no
network, no real LLM.
"""
from __future__ import annotations

import threading
import time

from aiforge_core.runtime import chat_approve, chat_cancel, tool_gate
from aiforge_core.runtime.diff_preview import unified_preview


class _FakeTool:
    def __init__(self, name):
        self.name = name


def _run(coro):
    import asyncio
    return asyncio.run(coro)


# ─── (a) review-edits flag ────────────────────────────────────────────

def test_review_flag_set_get_clear():
    sid = 7001
    assert chat_approve.review_edits(sid) is False
    chat_approve.set_review_edits(sid, True)
    assert chat_approve.review_edits(sid) is True
    chat_approve.set_review_edits(sid, False)
    assert chat_approve.review_edits(sid) is False


def test_review_flag_cleared_on_finish():
    sid = 7002
    chat_approve.set_review_edits(sid, True)
    assert chat_approve.review_edits(sid) is True
    chat_approve.finish(sid)
    assert chat_approve.review_edits(sid) is False


def test_review_flag_session_isolation():
    a, b = 7003, 7004
    chat_approve.set_review_edits(a, True)
    assert chat_approve.review_edits(a) is True
    assert chat_approve.review_edits(b) is False
    chat_approve.finish(a)


def test_review_flag_none_session_is_safe():
    assert chat_approve.review_edits(None) is False
    chat_approve.set_review_edits(None, True)   # no-op, must not raise


# ─── (b) tool gate forces approval under review-edits ─────────────────

def test_gate_review_on_with_emitter_forces_approval_and_rejects(monkeypatch):
    # policy ALLOW (default) but review-edits armed + emitter → must ASK.
    monkeypatch.delenv("AIFORGE_TOOL_POLICY", raising=False)
    sid = 7010
    events: list = []
    chat_approve.set_emitter(sid, events.append)
    chat_approve.set_review_edits(sid, True)
    chat_cancel.set_active(sid)

    def _auto():
        for _ in range(80):
            if chat_approve.resolve(sid, "reject"):
                return
            time.sleep(0.02)

    t = threading.Thread(target=_auto)
    t.start()
    cb = tool_gate.make_approval_gate_callback()
    out = _run(cb(tool=_FakeTool("file_write"),
                  args={"path": "a.txt", "content": "hi"}, tool_context=None))
    t.join(timeout=3)
    assert any(e.get("type") == "approval" for e in events)
    assert out and out.get("rejected") is True
    chat_approve.clear_emitter(sid)
    chat_approve.finish(sid)
    chat_cancel.set_active(None)


def test_gate_review_off_allows_through(monkeypatch):
    # Default behavior unchanged: flag off → mutating tool falls straight
    # through even with an emitter attached.
    monkeypatch.delenv("AIFORGE_TOOL_POLICY", raising=False)
    sid = 7011
    events: list = []
    chat_approve.set_emitter(sid, events.append)
    chat_approve.set_review_edits(sid, False)
    chat_cancel.set_active(sid)
    cb = tool_gate.make_approval_gate_callback()
    out = _run(cb(tool=_FakeTool("file_write"),
                  args={"path": "a.txt", "content": "hi"}, tool_context=None))
    assert out is None
    assert not events
    chat_approve.clear_emitter(sid)
    chat_approve.finish(sid)
    chat_cancel.set_active(None)


def test_gate_review_on_without_emitter_stays_autonomous(monkeypatch):
    # Review armed but NO approver attached (autonomous ticket run) → must not
    # hang; degrades to allow.
    monkeypatch.delenv("AIFORGE_TOOL_POLICY", raising=False)
    sid = 7012
    chat_approve.set_review_edits(sid, True)
    chat_cancel.set_active(sid)
    cb = tool_gate.make_approval_gate_callback()
    out = _run(cb(tool=_FakeTool("file_write"),
                  args={"path": "a.txt", "content": "hi"}, tool_context=None))
    assert out is None
    chat_approve.finish(sid)
    chat_cancel.set_active(None)


def test_gate_review_on_approve_lets_tool_proceed(monkeypatch):
    monkeypatch.delenv("AIFORGE_TOOL_POLICY", raising=False)
    sid = 7013
    chat_approve.set_emitter(sid, lambda e: None)
    chat_approve.set_review_edits(sid, True)
    chat_cancel.set_active(sid)

    def _auto():
        for _ in range(80):
            if chat_approve.resolve(sid, "approve"):
                return
            time.sleep(0.02)

    threading.Thread(target=_auto).start()
    cb = tool_gate.make_approval_gate_callback()
    out = _run(cb(tool=_FakeTool("file_write"),
                  args={"path": "a.txt", "content": "hi"}, tool_context=None))
    assert out is None   # approved → tool proceeds
    chat_approve.clear_emitter(sid)
    chat_approve.finish(sid)
    chat_cancel.set_active(None)


def test_gate_review_on_gates_doer_edit_aliases(monkeypatch):
    # H1 — the ADK Doer registers edit ALIASES (write→file_write,
    # patch/edit/str_replace→file_patch). Review-mode must gate those too, or a
    # mutating edit made via an alias would skip the human Approve/Reject.
    monkeypatch.delenv("AIFORGE_TOOL_POLICY", raising=False)
    for i, alias in enumerate(("write", "patch", "edit", "str_replace")):
        sid = 7030 + i
        events: list = []
        chat_approve.set_emitter(sid, events.append)
        chat_approve.set_review_edits(sid, True)
        chat_cancel.set_active(sid)

        def _auto(_sid=sid):
            for _ in range(80):
                if chat_approve.resolve(_sid, "reject"):
                    return
                time.sleep(0.02)

        t = threading.Thread(target=_auto)
        t.start()
        cb = tool_gate.make_approval_gate_callback()
        out = _run(cb(tool=_FakeTool(alias),
                      args={"path": "a.txt", "old_text": "x", "new_text": "y"},
                      tool_context=None))
        t.join(timeout=3)
        assert any(e.get("type") == "approval" for e in events), alias
        assert out and out.get("rejected") is True, alias
        chat_approve.clear_emitter(sid)
        chat_approve.finish(sid)
        chat_cancel.set_active(None)


def test_gate_review_ignores_nonmutating_tool(monkeypatch):
    # A read tool is never gated by review-edits, even with emitter + flag on.
    monkeypatch.delenv("AIFORGE_TOOL_POLICY", raising=False)
    sid = 7014
    events: list = []
    chat_approve.set_emitter(sid, events.append)
    chat_approve.set_review_edits(sid, True)
    chat_cancel.set_active(sid)
    cb = tool_gate.make_approval_gate_callback()
    out = _run(cb(tool=_FakeTool("file_read"),
                  args={"path": "a.txt"}, tool_context=None))
    assert out is None
    assert not events
    chat_approve.clear_emitter(sid)
    chat_approve.finish(sid)
    chat_cancel.set_active(None)


# ─── (d) inline simple-chat gate honors review-edits ──────────────────

def test_simple_chat_loop_review_on_forces_approval_then_reject(tmp_path):
    from aiforge_core.runtime import chat_agent as ca

    def _scripted(outputs):
        seq = list(outputs)
        return lambda role, messages, **kw: seq.pop(0)

    fn = _scripted([
        'ACTION: file_write\nARGS_JSON: {"path": "x.txt", "content": "hi"}',
        "FINAL: ok",
    ])
    sid = 7020
    chat_approve.set_review_edits(sid, True)   # policy is ALLOW by default

    def _auto_reject():
        for _ in range(60):
            if chat_approve.resolve(sid, "reject"):
                return
            time.sleep(0.03)

    t = threading.Thread(target=_auto_reject)
    t.start()
    evs = list(ca.run_chat_agent(
        [{"role": "user", "content": "write"}], cwd=str(tmp_path),
        complete_fn=fn, session_id=sid))
    t.join(timeout=3)
    assert any(e["type"] == "approval" for e in evs)
    tool = [e for e in evs if e["type"] == "tool"][0]
    assert tool["result"].get("rejected") is True
    assert not (tmp_path / "x.txt").exists()
    chat_approve.finish(sid)
    chat_cancel.set_active(None)


def test_simple_chat_loop_review_off_writes_without_approval(tmp_path):
    from aiforge_core.runtime import chat_agent as ca

    def _scripted(outputs):
        seq = list(outputs)
        return lambda role, messages, **kw: seq.pop(0)

    fn = _scripted([
        'ACTION: file_write\nARGS_JSON: {"path": "y.txt", "content": "hi"}',
        "FINAL: ok",
    ])
    sid = 7021
    chat_approve.set_review_edits(sid, False)   # default → no approval prompt
    evs = list(ca.run_chat_agent(
        [{"role": "user", "content": "write"}], cwd=str(tmp_path),
        complete_fn=fn, session_id=sid))
    assert not any(e["type"] == "approval" for e in evs)
    assert (tmp_path / "y.txt").read_text() == "hi"
    chat_approve.finish(sid)
    chat_cancel.set_active(None)


# ─── (c) unified_preview helper ───────────────────────────────────────

def test_unified_preview_existing_file(tmp_path):
    f = tmp_path / "code.py"
    f.write_text("print('old')\nx = 1\n")
    diff = unified_preview("code.py", "print('new')\nx = 1\n", str(tmp_path))
    assert "-print('old')" in diff
    assert "+print('new')" in diff


def test_unified_preview_new_file(tmp_path):
    # File does not exist → preview still useful (new-file diff with + lines).
    out = unified_preview("brand_new.py", "a = 1\nb = 2\n", str(tmp_path))
    assert "brand_new.py" in out or "+a = 1" in out
    assert "a = 1" in out


def test_unified_preview_caps_length(tmp_path):
    big = "x\n" * 5000
    out = unified_preview("big.py", big, str(tmp_path), max_chars=500)
    assert len(out) <= 500


def test_unified_preview_resolves_repo_root(tmp_path, monkeypatch):
    # Path resolves against AIFORGE_REPO_ROOT when set (the Doer's repo).
    f = tmp_path / "r.py"
    f.write_text("one\n")
    monkeypatch.setenv("AIFORGE_REPO_ROOT", str(tmp_path))
    diff = unified_preview("r.py", "two\n", "")
    assert "-one" in diff and "+two" in diff
