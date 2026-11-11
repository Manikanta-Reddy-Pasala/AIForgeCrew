"""The answer's code is kept at the first send-back and comes back when the
extra round makes the tests worse (attrs: 13/18 at the send-back, 0/18 after)."""
from __future__ import annotations

import subprocess
from types import SimpleNamespace

from aiforge_core.runtime.chat_agent._turn import _checkpoint as C

GIT = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]


def _repo(tmp_path):
    (tmp_path / "pkg.py").write_text("A = 1\n")
    (tmp_path / "old.py").write_text("B = 2\n")
    (tmp_path / ".gitignore").write_text("build/\n")
    for c in (["git", "init", "-q"], ["git", "add", "-A"], GIT + ["commit", "-qm", "seed"]):
        subprocess.run(c, cwd=tmp_path, check=True, capture_output=True)
    return tmp_path


def test_tree_round_trip_restores_edits_new_and_removed_files(tmp_path):
    r = _repo(tmp_path)
    (r / "pkg.py").write_text("A = 2  # the answer\n")
    (r / "new_answer.py").write_text("C = 3\n")
    saved = C.tree_of(str(r))
    status_before = subprocess.run(["git", "status", "--porcelain"], cwd=r,
                                   capture_output=True, text=True).stdout
    # the extra round: breaks pkg, deletes a file, adds another, makes build output
    (r / "pkg.py").write_text("A = (\n")
    (r / "old.py").unlink()
    (r / "later.py").write_text("D = 4\n")
    (r / "build").mkdir()
    (r / "build" / "out.txt").write_text("x")
    assert C.restore_tree(str(r), saved)
    assert (r / "pkg.py").read_text() == "A = 2  # the answer\n"
    assert (r / "old.py").read_text() == "B = 2\n"
    assert (r / "new_answer.py").exists() and not (r / "later.py").exists()
    assert (r / "build" / "out.txt").exists()                  # ignored files untouched
    assert subprocess.run(["git", "status", "--porcelain"], cwd=r, capture_output=True,
                          text=True).stdout == status_before   # user's index untouched


def _st(r, **kw):
    return SimpleNamespace(**{"cwd": str(r), "edits_made": 2, **kw})


def test_worse_after_the_send_back_is_restored_with_a_note(tmp_path, monkeypatch):
    r = _repo(tmp_path)
    (r / "pkg.py").write_text("A = 2\n")
    counts = iter([5, 999])                    # at the send-back, then at the answer
    monkeypatch.setattr(C, "_failures", lambda st, cwd: next(counts))
    st = _st(r)
    C.save(st, str(r), "the tests check")
    (r / "pkg.py").write_text("A = (\n")
    note = C.restore_if_worse(st, str(r))
    assert "put the code back" in note and "could not run" in note and "5 failing" in note
    assert (r / "pkg.py").read_text() == "A = 2\n"


def test_better_or_equal_or_unchanged_is_kept(tmp_path, monkeypatch):
    r = _repo(tmp_path)
    (r / "pkg.py").write_text("A = 2\n")
    for before, after in ((5, 3), (5, 5)):
        counts = iter([before, after])
        monkeypatch.setattr(C, "_failures", lambda st, cwd: next(counts))
        st = _st(r)
        C.save(st, str(r))
        (r / "pkg.py").write_text(f"A = {after}\n")
        assert C.restore_if_worse(st, str(r)) == ""
        assert (r / "pkg.py").read_text() == f"A = {after}\n"
    calls = []
    monkeypatch.setattr(C, "_failures", lambda st, cwd: calls.append(1) or 1)
    st = _st(r)
    C.save(st, str(r))
    assert C.restore_if_worse(st, str(r)) == "" and len(calls) == 1   # no change: no rerun


def test_saved_once_per_turn_and_never_without_edits_or_a_verdict(tmp_path, monkeypatch):
    r = _repo(tmp_path)
    monkeypatch.setattr(C, "_failures", lambda st, cwd: 1)
    st = _st(r, edits_made=0)
    C.save(st, str(r))
    assert getattr(st, "checkpoint", None) is None
    st = _st(r)
    C.save(st, str(r))
    first = st.checkpoint["tree"]
    (r / "pkg.py").write_text("A = 9\n")
    C.save(st, str(r))
    assert st.checkpoint["tree"] == first
    monkeypatch.setattr(C, "_failures", lambda st, cwd: None)
    st = _st(r)
    C.save(st, str(r))
    assert st.checkpoint == {}


def test_off_switch(tmp_path, monkeypatch):
    r = _repo(tmp_path)
    monkeypatch.setenv("AIFORGE_CHAT_CHECKPOINT", "0")
    st = _st(r)
    C.save(st, str(r))
    assert getattr(st, "checkpoint", None) is None
