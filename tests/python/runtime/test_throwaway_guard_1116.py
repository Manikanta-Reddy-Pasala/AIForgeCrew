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
