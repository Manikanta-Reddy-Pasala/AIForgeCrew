"""The throwaway files a turn made leave the repo at its end (moved, not deleted)."""
import os
import subprocess
from types import SimpleNamespace

from aiforge_core.runtime.chat_agent._turn import _scratch_tidy as T


def _repo(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    r = tmp_path / "r"
    (r / "src").mkdir(parents=True)
    (r / "src/lib.rs").write_text("pub fn f() {}\n")
    (r / "notes_mine.txt").write_text("the user's own untracked file\n")
    g = lambda *a: subprocess.run(["git", "-C", str(r), *a], check=True, capture_output=True)
    g("init", "-q"); g("add", "src"); g("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "s")
    return r


def _st(r, goal="fix the partial upper bound"):
    from aiforge_core.runtime.chat_agent._turn._example_gate import note_untracked
    st = SimpleNamespace(goal=goal)
    note_untracked(st, str(r))
    return st


def test_probe_sources_and_their_binaries_are_moved_out(tmp_path, monkeypatch):
    r = _repo(tmp_path, monkeypatch)
    st = _st(r)
    for i in ("", "2", "13"):
        (r / f"test_partial_upper_bug{i}.rs").write_text("fn main(){}\n")
        b = r / f"test_partial_upper_bug{i}"
        b.write_bytes(b"\x7fELF")
        b.chmod(0o755)
    (r / "debug_parse.py").write_text("print(1)\n")
    (r / "src/new_module.rs").write_text("pub fn g() {}\n")          # real work
    (r / "tests").mkdir()
    (r / "tests/test_bug_regression.rs").write_text("#[test] fn t(){}\n")  # the repo's test
    ev = T.tidy(st, str(r))
    assert ev and "moved 7 throwaway file(s)" in ev["text"]
    left = sorted(p.name for p in r.iterdir() if p.is_file())
    assert left == ["notes_mine.txt"]
    assert (r / "src/new_module.rs").exists() and (r / "tests/test_bug_regression.rs").exists()
    trash = tmp_path / "cfg" / "trash"
    assert any(f.name == "test_partial_upper_bug13.rs" for f in trash.rglob("*"))


def test_files_the_request_names_or_that_were_there_stay(tmp_path, monkeypatch):
    r = _repo(tmp_path, monkeypatch)
    (r / "debug_old.py").write_text("x\n")                     # there before the turn
    st = _st(r, goal="add a debug_dump.py script that prints the table")
    (r / "debug_dump.py").write_text("print()\n")              # the request names it
    assert T.tidy(st, str(r)) is None
    assert (r / "debug_old.py").exists() and (r / "debug_dump.py").exists()


def test_off_switch_and_no_snapshot(tmp_path, monkeypatch):
    r = _repo(tmp_path, monkeypatch)
    st = _st(r)
    (r / "repro_x.py").write_text("x\n")
    monkeypatch.setenv("AIFORGE_CHAT_SCRATCH_TIDY", "0")
    assert T.tidy(st, str(r)) is None
    monkeypatch.delenv("AIFORGE_CHAT_SCRATCH_TIDY")
    assert T.tidy(SimpleNamespace(goal=""), str(r)) is None   # no start snapshot
    assert (r / "repro_x.py").exists()
