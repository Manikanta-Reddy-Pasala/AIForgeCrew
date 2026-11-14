"""After a condense the model gets its own edits back as diffs."""
from __future__ import annotations

import subprocess
from types import SimpleNamespace

from aiforge_core.runtime.chat_agent._turn import _workset as W


def _repo(tmp_path):
    r = tmp_path / "proj"
    r.mkdir()
    (r / "parser.py").write_text("".join(f"line {i}\n" for i in range(200)))
    g = lambda *a: subprocess.run(["git", "-C", str(r), *a], check=True, capture_output=True)
    g("init", "-q"); g("add", "-A"); g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "b")
    return r


def test_edited_files_come_back_as_diffs_with_broken_tests(tmp_path):
    r = _repo(tmp_path)
    text = (r / "parser.py").read_text().replace("line 120\n", "line 120 CHANGED\n")
    (r / "parser.py").write_text(text)
    (r / "new_mod.py").write_text("def f():\n    return 1\n")
    st = SimpleNamespace(cwd=str(r), file_hashes={str(r / "parser.py"): "x", str(r / "new_mod.py"): "y"},
                         regress_said={"tests/test_a.py::test_x"})
    out = W.note(st)
    assert "line 120 CHANGED" in out and "line 100" in out and "line 10\n" not in out   # 30 lines context
    assert "new_mod.py (new file" in out and "def f():" in out
    assert "tests/test_a.py::test_x" in out and "do not re-read" in out


def test_bounded_and_switchable(tmp_path, monkeypatch):
    r = _repo(tmp_path)
    (r / "parser.py").write_text("X\n" * 5000)
    st = SimpleNamespace(cwd=str(r), file_hashes={str(r / "parser.py"): "x"})
    monkeypatch.setenv("AIFORGE_CHAT_WORKSET_CHARS", "2000")
    out = W.note(st)
    assert "not shown for room" in out and len(out) < 2500
    monkeypatch.setenv("AIFORGE_CHAT_CONDENSE_WORKSET", "0")
    assert W.note(st) == ""


def test_nothing_edited_nothing_said(tmp_path):
    r = _repo(tmp_path)
    assert W.note(SimpleNamespace(cwd=str(r), file_hashes={})) == ""
