"""Doer agents inside a Simple-mode turn: independent, file-disjoint parts run
side by side, dependent or file-sharing ones later; each doer is limited to
its files; doers never start doers and end before the lead's deadline."""
from __future__ import annotations

import threading
import time

import pytest

from aiforge_core.runtime.chat_agent._tools import _doers as D


@pytest.fixture(autouse=True)
def _clean_doer_context(monkeypatch):
    # an earlier chat test leaves its turn deadline set on this thread
    toks = [(v, v.set(d)) for v, d in ((D.LEAD_DEADLINE, None), (D.IN_DOER, False),
                                       (D.DOER_BUDGET_S, None), (D.LEAD_SID, None))]
    from aiforge_core.runtime import request_context
    monkeypatch.setattr(request_context, "get_session_id", lambda: None)
    yield
    for v, t in reversed(toks):
        v.reset(t)


def _p(title, files, after=()):
    return {"title": title, "task": "x" * 30, "files": list(files), "after": list(after)}


def test_waves_put_independent_parts_together_and_dependent_ones_later():
    parts = [_p("a", ["a.py"]), _p("b", ["b.py"]), _p("c", ["c.py"], after=["a"]),
             _p("d", ["a.py"])]
    got = [[p["title"] for p in w] for w in D.waves(parts)]
    assert got[0] == ["a", "b"]                  # independent, disjoint
    assert "c" in got[1] and "d" in got[1]       # c waits on a; d shares a.py with a
    assert len({t for w in got for t in w}) == 4


def test_parts_are_checked():
    assert D._parts({})[1]
    assert "files" in D._parts({"parts": [{"title": "a", "task": "x" * 30}, _p("z", ["z.py"])]})[1]
    assert "full task" in D._parts({"parts": [{"title": "a", "task": "do", "files": ["a"]}, _p("z", ["z.py"])]})[1]
    assert "after names no part" in D._parts({"parts": [
        {"title": "a", "task": "x" * 30, "files": ["a"], "after": ["zz"]}, _p("z", ["z.py"])]})[1]
    assert "at most" in D._parts({"parts": [_p(str(i), [f"{i}.py"]) for i in range(5)]})[1]
    ok, why = D._parts({"parts": [{"title": "a", "task": "x" * 30, "files": ["./src/a.py"]}, _p("z", ["z.py"])]})
    assert not why and ok[0]["files"] == ["src/a.py"]
    assert "do it yourself" in D._parts({"parts": [_p("a", ["a.py"])]})[1]   # one part: no doer


def test_a_doer_cannot_start_doers_and_no_time_means_no_doers(monkeypatch):
    tok = D.IN_DOER.set(True)
    try:
        assert "own part" in D._t_run_doers({"parts": [_p("a", ["a.py"]), _p("z", ["z.py"])]}, ".")["error"]
    finally:
        D.IN_DOER.reset(tok)
    tok = D.LEAD_DEADLINE.set(time.monotonic() + 100)
    try:
        assert "time" in D._t_run_doers({"parts": [_p("a", ["a.py"]), _p("z", ["z.py"])]}, ".")["error"]
    finally:
        D.LEAD_DEADLINE.reset(tok)


def _fake_snapshot():
    seen = {}

    def snap(cwd, files):
        k = tuple(files)
        seen[k] = seen.get(k, 0) + 1
        return {f: ("before" if seen[k] == 1 else "after") for f in files}
    return snap


def test_doers_run_with_their_files_and_report(monkeypatch):
    import aiforge_core.runtime.chat_agent as ca
    seen, live, peak = [], [0], [0]
    lock = threading.Lock()

    def fake(messages, **kw):
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        seen.append((kw["scope_globs"], kw["session_id"], D.IN_DOER.get(), D.DOER_BUDGET_S.get()))
        time.sleep(0.05)
        path = kw["scope_globs"][0]
        yield {"type": "tool", "name": "file_write", "args": {"path": path, "content": "x"},
               "result": {"ok": True}}
        yield {"type": "message", "text": f"FINAL: wrote {path}"}
        with lock:
            live[0] -= 1
    monkeypatch.setattr(ca, "run_chat_agent", fake)
    monkeypatch.setattr(D, "_snapshot", lambda cwd, files, _c={}: (
        _c.setdefault(tuple(files), 0) or _c.__setitem__(tuple(files), 1)
        or {f: "before" for f in files}) if not _c.get(("done",) + tuple(files)) else {})
    monkeypatch.setattr(D, "_snapshot", _fake_snapshot())
    monkeypatch.setattr(D, "_dirty", lambda cwd: set())
    monkeypatch.setenv("AIFORGE_DOERS_PARALLEL", "2")
    out = D._t_run_doers({"parts": [_p("a", ["a.py"]), _p("b", ["b.py"]),
                                    _p("c", ["c.py"], after=["a"])]}, ".")
    assert out["ok"] and out["waves"] == [["a", "b"], ["c"]]
    assert [r["files_changed"] for r in out["results"]] == [["a.py"], ["b.py"], ["c.py"]]
    assert peak[0] == 2                                   # a and b side by side
    assert all(s[1] is None and s[2] is True and s[3] for s in seen)   # no session, in-doer
    assert not D.IN_DOER.get() and D.LEAD_SID.get() is None   # the lead's context is untouched


def test_a_crashing_doer_is_its_own_result(monkeypatch):
    import aiforge_core.runtime.chat_agent as ca

    def boom(messages, **kw):
        raise RuntimeError("model down")
        yield  # pragma: no cover
    monkeypatch.setattr(ca, "run_chat_agent", boom)
    out = D._t_run_doers({"parts": [_p("a", ["a.py"]), _p("z", ["z.py"])]}, ".")
    assert not out["ok"] and "model down" in out["results"][0]["error"]


def test_the_tool_is_in_the_simple_mode_core_set():
    from aiforge_core.runtime.chat_agent import _registry
    from aiforge_core.runtime.chat_agent._tools import _schemas
    assert "run_doers" in _registry.TOOLS if hasattr(_registry, "TOOLS") else True
    assert "run_doers" in _schemas._CORE_ACT


def test_changed_files_come_from_git_within_the_parts_files(tmp_path):
    import subprocess
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "b.py").write_text("y = 1\n")
    for c in (["init", "-q"], ["add", "-A"], ["-c", "user.email=t@t", "-c", "user.name=t",
                                              "commit", "-qm", "s"]):
        subprocess.run(["git", "-C", str(tmp_path), *c], check=True, capture_output=True)
    (tmp_path / "a.py").write_text("x = 2\n")
    (tmp_path / "b.py").write_text("y = 2\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("")
    assert D._changed_in(str(tmp_path), ["a.py", "tests"]) == ["a.py", "tests/test_a.py"]


def test_dot_paths_keep_their_dot_and_outside_paths_are_refused():
    ok, why = D._parts({"parts": [{"title": "a", "task": "x" * 30,
                                   "files": ["./.github/ci.yml", ".env"]}, _p("z", ["z.py"])]})
    assert not why and ok[0]["files"] == [".github/ci.yml", ".env"]
    assert "inside the repository" in D._parts({"parts": [
        {"title": "a", "task": "x" * 30, "files": ["../x.py"]}, _p("z", ["z.py"])]})[1]


def test_a_doer_stops_when_the_lead_chat_is_stopped(monkeypatch):
    from aiforge_core.runtime import chat_cancel
    from aiforge_core.runtime.chat_agent import _loop
    tok = D.LEAD_SID.set(77)
    try:
        monkeypatch.setattr(chat_cancel, "is_cancelled", lambda sid: sid == 77)
        assert _loop._lead_stopped()
        monkeypatch.setattr(chat_cancel, "is_cancelled", lambda sid: False)
        assert not _loop._lead_stopped()
    finally:
        D.LEAD_SID.reset(tok)


def test_approvals_on_means_no_doers(monkeypatch):
    from aiforge_core.runtime import request_context
    monkeypatch.setattr(request_context, "get_session_id", lambda: 5)
    monkeypatch.setattr(D, "_approvals_on", lambda sid: True)
    out = D._t_run_doers({"parts": [_p("a", ["a.py"]), _p("z", ["z.py"])]}, ".")
    assert not out["ok"] and "approvals" in out["error"]


def test_absolute_paths_under_the_repo_become_relative(monkeypatch, tmp_path):
    import aiforge_core.runtime.chat_agent as ca
    got = []
    monkeypatch.setattr(ca, "run_chat_agent", lambda m, **kw: got.append(kw["scope_globs"]) or iter(
        [{"type": "message", "text": "FINAL: ok"}]))
    monkeypatch.setattr(D, "_dirty", lambda cwd: set())
    D._t_run_doers({"parts": [{"title": "a", "task": "x" * 30,
                               "files": [str(tmp_path / "src" / "a.py")]},
                              {"title": "b", "task": "x" * 30,
                               "files": [str(tmp_path / "b.py")]}]}, str(tmp_path))
    assert sorted(g[0] for g in got) == ["b.py", "src/a.py"]
