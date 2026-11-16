"""Throwaway probe programs: one note after the third, spin counted after."""
from types import SimpleNamespace

from aiforge_core.runtime.chat_agent._turn import _throwaway_guard as G


def _st():
    st = SimpleNamespace(cwd="/r")
    st.held = []
    return st


def test_note_after_third_throwaway(monkeypatch):
    from aiforge_core.runtime.chat_agent._turn import _notes
    st = _st()
    monkeypatch.setattr(_notes, "hold", lambda s, t: s.held.append(t))
    G.check(st, "run_command", {"cmd": "cd /r && cat > /tmp/test_bug.rs << 'EOF'\nfn main(){}\nEOF"})
    G.check(st, "file_write", {"path": "/r/examples/debug_less_than.rs"})
    assert st.held == []
    G.check(st, "file_write", {"path": "/r/examples/analyze_ge3.rs"})
    assert len(st.held) == 1 and "throwaway" in st.held[0]
    G.check(st, "file_write", {"path": "/r/examples/try_four.rs"})
    assert len(st.held) == 1 and st.spin_events >= 2


def test_real_source_and_test_files_do_not_count(monkeypatch):
    from aiforge_core.runtime.chat_agent._turn import _notes
    st = _st()
    monkeypatch.setattr(_notes, "hold", lambda s, t: s.held.append(t))
    for p in ("src/eval.rs", "tests/test_matches.rs", "src/lib.rs", "src/main/java/a/Foo.java"):
        G.check(st, "file_write", {"path": p})
    assert st.held == [] and not getattr(st, "probe_files", [])


def test_refused_after_the_note(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from aiforge_core.runtime.chat_agent._turn import _throwaway_guard as g
    monkeypatch.delenv("AIFORGE_CHAT_THROWAWAY_GUARD", raising=False)
    st = SimpleNamespace(cwd=str(tmp_path), spin_events=0)
    probe = {"cmd": "cat > /tmp/test9.rs << 'EOF'\nfn main(){}\nEOF"}
    assert g.refusal(st, "run_command", probe) is None          # before the note
    for i in range(3):
        g.check(st, "run_command", {"cmd": f"cat > /tmp/t{i}.rs << 'EOF'\nEOF"})
    assert st.probe_said
    r = g.refusal(st, "run_command", probe)
    assert r and r["ok"] is False and "test9.rs" in r["error"]
    # the repo's own test, and non-source files, are never refused
    assert g.refusal(st, "file_write", {"path": str(tmp_path / "tests" / "t_req.rs")}) is None
    assert g.refusal(st, "run_command", {"cmd": "cat > /tmp/out.txt << 'EOF'\nEOF"}) is None
