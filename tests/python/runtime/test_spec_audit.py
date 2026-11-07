"""A big request's answer gets one fresh read of the spec against the change
(attrs n2: the code and its own tests said on_setattr_hook=None where the
spec said define()'s default pipe; 11/18)."""
from __future__ import annotations

import time
from types import SimpleNamespace

from aiforge_core.runtime.chat_agent._turn import _spec_audit as A


def _run(g):
    out = []
    try:
        while True:
            out.append(next(g))
    except StopIteration as e:
        return out, e.value


def test_only_spec_code_lines_count():
    reply = ('Let me think.\n'
             'SPEC: "the default convert+validate pipe that define() installs" | CODE: '
             'returns None for the default hook (src/attr/_make.py: _props)\n'
             'SPEC: "fields are bools" | CODE: matches the spec, this is fine (x.py: y)\n')
    rows = A.findings(reply)
    assert len(rows) == 1 and "returns None" in rows[0]
    assert A.findings("NONE") == [] and A.findings("") == []
    assert len(A.findings('1. SPEC: "a" | CODE: b is not consistent with it (x.py: f)')) == 1
    assert len(A.findings('**SPEC:** "a" | **CODE:** b (x.py: f)')) == 1


def _st(**kw):
    base = dict(goal="x" * 2000, edits_made=3, board={}, turn_deadline=time.monotonic() + 1000,
                turn_budget_s=3000, convo=[], session_id=None, role="chat")
    base.update(kw)
    return SimpleNamespace(**base)


def test_findings_go_back_once(monkeypatch):
    monkeypatch.setattr(A, "change_text", lambda st: "diff --git a b")
    monkeypatch.setattr(A, "audit", lambda *a, **k: ['"x" — code: y'])
    st = _st()
    out, sig = _run(A.gate(st, {"text": "FINAL: done"}))
    assert sig == "continue" and "fresh read" in st.convo[-1]["content"]
    assert _run(A.gate(st, {"text": "FINAL"}))[1] is None          # once a turn


def test_small_late_or_unchanged_turns_are_let_through(monkeypatch):
    monkeypatch.setattr(A, "change_text", lambda st: "diff")
    monkeypatch.setattr(A, "audit", lambda *a, **k: ["x"])
    assert _run(A.gate(_st(goal="fix it"), {}))[1] is None          # small request
    assert _run(A.gate(_st(turn_deadline=time.monotonic() + 60), {}))[1] is None   # no time
    assert _run(A.gate(_st(edits_made=0), {}))[1] is None
    assert _run(A.gate(_st(), {}, builder="job"))[1] is None
    monkeypatch.setattr(A, "audit", lambda *a, **k: [])
    assert _run(A.gate(_st(), {}))[1] is None                     # NONE: through
