"""A read of lines already shown (shifted a line or two) is a repeat, and
skipped repeats in a row hand over to the stuck recovery (click a15: lines
1727-1737 of core.py read ~30 times each, 340 reads, 6/37)."""
from __future__ import annotations

from types import SimpleNamespace

from aiforge_core.runtime.chat_agent._turn import _action as A


def _st():
    return SimpleNamespace(read_sigs_seen=set(), read_windows={})


def test_window_from_offset_limit_and_start_end():
    assert A._window("file_read", {"path": "a/core.py", "offset": 1730, "limit": 40}) == \
        ("a/core.py", 1730, 1769)
    assert A._window("read_lines", {"path": "x.py", "start": 5, "end": 9}) == ("x.py", 5, 9)
    assert A._window("file_read", {"path": "x.py"}) is None          # whole file
    assert A._window("grep", {"path": "x.py", "start": 1}) is None


def test_a_shifted_range_already_shown_is_covered():
    st = _st()
    st.read_sigs_seen.add("s1")
    st.read_windows["s1"] = ("a/core.py", 1727, 1766)
    assert A._covered(st, "file_read", {"path": "a/core.py", "offset": 1730, "limit": 40})
    assert not A._covered(st, "file_read", {"path": "a/core.py", "offset": 1760, "limit": 40})
    assert not A._covered(st, "file_read", {"path": "b.py", "offset": 1730, "limit": 40})


def test_a_forgotten_or_edited_read_no_longer_covers():
    st = _st()
    st.read_windows["s1"] = ("a/core.py", 1727, 1766)      # its sig aged out / an edit cleared it
    assert not A._covered(st, "file_read", {"path": "a/core.py", "offset": 1730, "limit": 40})


def test_only_the_lines_the_result_holds_are_recorded():
    st = SimpleNamespace(read_sigs_seen=set(), read_sigs_ever={}, read_windows={}, reads_new=0)
    text = "".join(f"l{i}\n" for i in range(1, 601))          # cut at 600 of 2000 lines
    A._record_read(st, "read_lines", "s1", {"ok": True, "start": 1, "end": 2000, "text": text},
                   True, {"path": "core.py", "start": 1, "end": 2000})
    assert st.read_windows["s1"] == ("core.py", 1, 600)
    assert not A._covered(st, "read_lines", {"path": "core.py", "start": 1000, "end": 1100})
    A._record_read(st, "read_lines", "s2", {"ok": True, "total_lines": 10, "text": ""},
                   True, {"path": "core.py", "start": 50, "end": 60})
    assert "s2" not in st.read_windows


def test_the_unseen_lines_are_named():
    st = _st()
    st.read_sigs_seen.add("s1")
    st.read_windows["s1"] = ("a.py", 1, 100)
    assert A._covered(st, "read_lines", {"path": "a.py", "start": 1, "end": 120})
    assert A._unseen(st, "read_lines", {"path": "a.py", "start": 1, "end": 120}) == "101-120"


def test_a_dedupe_pointer_is_not_recorded_as_shown():
    st = SimpleNamespace(read_sigs_seen=set(), read_sigs_ever={}, read_windows={}, reads_new=0)
    A._record_read(st, "read_lines", "s1",
                   {"ok": True, "start": 1, "end": 50,
                    "text": "[already in context: file a.py sha256:0123456789abcdef — same text is above; not repeated]"},
                   True, {"path": "a.py", "start": 1, "end": 50})
    assert "s1" not in st.read_windows


def test_a_failed_edit_lets_the_file_be_read_again():
    """sqlglot a5: patches failed on stale text, every re-read was skipped."""
    st = _st()
    st.read_sigs_seen.update({"s1", "s2"})
    st.read_windows.update({"s1": ("sqlglot/parser.py", 9400, 9460), "s2": ("other.py", 1, 40)})
    A._forget_after_failed_edit(st, "file_patch", {"path": "sqlglot/parser.py"},
                                {"ok": False, "error": "old_str not found"})
    assert "s1" not in st.read_windows and "s1" not in st.read_sigs_seen
    assert "s2" in st.read_windows
    assert not A._covered(st, "read_lines", {"path": "sqlglot/parser.py", "start": 9440, "end": 9450})
    A._forget_after_failed_edit(st, "file_patch", {"path": "other.py"}, {"ok": True})
    assert "s2" in st.read_windows


def test_a_failed_multi_edit_frees_whole_file_reads_of_its_files_only():
    st = _st()
    st.read_sigs_seen.update({'file_read:{"path": "pkg/a.py"}', 'file_read:{"path": "pkg/b.py"}'})
    A._forget_after_failed_edit(st, "multi_edit", {"edits": [{"path": "pkg/a.py", "old": "x"}]},
                                {"ok": False})
    assert st.read_sigs_seen == {'file_read:{"path": "pkg/b.py"}'}
    A._forget_after_failed_edit(st, "multi_edit", {"edits": []}, {"ok": False})
    assert st.read_sigs_seen == {'file_read:{"path": "pkg/b.py"}'}       # no path: nothing dropped
