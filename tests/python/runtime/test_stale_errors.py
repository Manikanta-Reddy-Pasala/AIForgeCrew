"""An error the post-edit check named, still in the file steps later, is
said again (attrs n3: `_make_class_props` undefined in _make.py, said
twice, then 30 minutes of reads with the package unable to import, 0/18)."""
from __future__ import annotations

from types import SimpleNamespace

from aiforge_core.runtime import post_edit
from aiforge_core.runtime.chat_agent._turn import _post_edit as P

_ERR = ["src/m.py:3:5: F821 Undefined name `_make_props`"]


def _st(step):
    return SimpleNamespace(step_n=step, convo=[{"role": "assistant", "content": "x"}])


def test_an_error_left_for_steps_is_said_again_once(monkeypatch, tmp_path):
    monkeypatch.setattr(post_edit, "diagnostics", lambda full, cwd: list(_ERR))
    monkeypatch.setattr(P, "_new_since_base", lambda st, full, cwd, key: list(key))
    monkeypatch.setattr(P, "_where_defined", lambda errs, full: [])
    st = _st(10)
    list(P.post_edit_diagnostics(st, [("src/m.py", "/r/src/m.py")], "/r"))
    st.step_n = 12
    assert list(P.stale_errors(st, "/r")) == []                # too soon
    st.step_n = 10 + P.STALE_STEPS
    out = list(P.stale_errors(st, "/r"))
    assert out and "still has" in out[0]["text"] and "cannot be imported" in st.convo[-1]["content"]
    st.step_n += 5
    assert list(P.stale_errors(st, "/r")) == []                # once


def test_a_fixed_file_is_not_said_again(monkeypatch):
    errs = {"now": list(_ERR)}
    monkeypatch.setattr(post_edit, "diagnostics", lambda full, cwd: errs["now"])
    monkeypatch.setattr(P, "_where_defined", lambda e, f: [])
    monkeypatch.setattr(P, "_new_since_base", lambda st, full, cwd, key: list(key))
    st = _st(1)
    list(P.post_edit_diagnostics(st, [("src/m.py", "/r/src/m.py")], "/r"))
    errs["now"] = []
    st.step_n = 1 + P.STALE_STEPS
    assert list(P.stale_errors(st, "/r")) == [] and not st.open_errors


def test_errors_the_file_had_at_the_base_are_not_tracked(monkeypatch):
    monkeypatch.setattr(post_edit, "diagnostics", lambda full, cwd: list(_ERR))
    monkeypatch.setattr(P, "_where_defined", lambda e, f: [])
    monkeypatch.setattr(P, "_new_since_base", lambda st, full, cwd, key: [])   # old errors
    st = _st(1)
    list(P.post_edit_diagnostics(st, [("src/m.py", "/r/src/m.py")], "/r"))
    assert not getattr(st, "open_errors", {})
