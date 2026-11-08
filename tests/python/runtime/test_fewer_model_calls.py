"""Fewer model calls for the same work: an edit shows the lines it changed,
read_lines reads several parts of a file at once, and a small change whose
own tests passed after the last edit skips the end-of-turn suite."""
from __future__ import annotations

import subprocess
from types import SimpleNamespace

from aiforge_core.runtime.chat_agent import _shell
from aiforge_core.runtime.chat_agent._tools import _code, _edit_view, _skills
from aiforge_core.runtime.chat_agent._turn import _outcomes


# ── A: an edit returns the lines it changed ──────────────────────────────

def test_view_is_the_changed_lines_with_a_little_context():
    before = "".join(f"line {i}\n" for i in range(1, 41))
    after = before.replace("line 20\n", "line 20\nadded a\nadded b\n")
    v = _edit_view.view(before, after)
    assert v["start"] == 18 and "added a\nadded b\n" in v["text"]
    assert v["text"].startswith("line 18\n") and v["text"].endswith("line 23\n")
    assert _edit_view.view(before, before) is None


def test_a_big_change_is_cut_and_says_so(monkeypatch):
    before = "x\n"
    after = "".join(f"new {i}\n" for i in range(200))
    v = _edit_view.view(before, after)
    assert v["end"] - v["start"] + 1 <= _edit_view._MAX_LINES and "cut" in v["note"]
    monkeypatch.setenv("AIFORGE_CHAT_EDIT_VIEW", "0")
    assert _edit_view.view(before, after) is None


def test_file_patch_and_multi_edit_return_the_view(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("one\ntwo\nthree\n")
    out = _shell._t_file_patch({"path": "a.txt", "old_text": "two", "new_text": "TWO"},
                               str(tmp_path))
    assert out["ok"] and "TWO" in out["now_reads"]["text"]
    out = _skills._t_multi_edit({"edits": [{"path": "a.txt", "old_str": "three",
                                            "new_str": "THREE"}]}, str(tmp_path))
    assert out["ok"] and "THREE" in out["now_reads"]["a.txt"]["text"]


# ── B: several ranges in one read ────────────────────────────────────────

def test_read_lines_takes_several_ranges(tmp_path):
    (tmp_path / "f.py").write_text("".join(f"L{i}\n" for i in range(1, 101)))
    out = _code._t_read_lines({"path": "f.py", "ranges": [[50, 52], {"start": 10, "end": 11}]},
                              str(tmp_path))
    assert out["ok"] and out["total_lines"] == 100
    assert [(p["start"], p["end"]) for p in out["parts"]] == [(10, 11), (50, 52)]
    assert out["parts"][1]["text"] == "L50\nL51\nL52\n"
    one = _code._t_read_lines({"path": "f.py", "start": 3, "end": 4}, str(tmp_path))
    assert one["text"] == "L3\nL4\n"                       # the plain form is unchanged


def test_the_model_sees_each_part_numbered(tmp_path):
    from aiforge_core.runtime.chat_agent._obs_text import render_read
    (tmp_path / "f.py").write_text("".join(f"L{i}\n" for i in range(1, 101)))
    out = _code._t_read_lines({"path": "f.py", "ranges": [[50, 51], [10, 10]]}, str(tmp_path))
    text = render_read(out, 10000)
    assert "10|L10" in text and "50|L50" in text and "51|L51" in text
    assert "no lines there" not in text


def test_bad_ranges_fall_back_to_the_plain_read(tmp_path):
    (tmp_path / "f.py").write_text("a\nb\n")
    out = _code._t_read_lines({"path": "f.py", "ranges": "nope"}, str(tmp_path))
    assert out["text"] == "a\nb\n"


# ── C: a small, tested change skips the end-of-turn suite ────────────────

def _repo(tmp_path):
    for c in (["init", "-q"], ["add", "-A"], ["-c", "user.email=t@t", "-c", "user.name=t",
                                              "commit", "-qm", "s"]):
        subprocess.run(["git", "-C", str(tmp_path), *c], check=True, capture_output=True)
    return subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()


def _green_run(st, tmp_path):
    _outcomes._note_green_tests(
        st, "run_command", {"cmd": "python -m pytest tests/test_a.py -q"},
        {"ok": True, "stdout": "....\n4 passed in 0.1s\n"}, str(tmp_path))


def test_small_change_tested_after_its_last_edit_skips(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    head = _repo(tmp_path)
    (tmp_path / "a.py").write_text("x = 2\ny = 3\n")
    st = SimpleNamespace(head0=head, cwd=str(tmp_path), edits_made=1)
    assert not _outcomes._small_and_tested(st, str(tmp_path))      # not tested yet
    _green_run(st, tmp_path)
    assert _outcomes._small_and_tested(st, str(tmp_path))
    (tmp_path / "a.py").write_text("x = 3\n")                       # edited after the run
    assert not _outcomes._small_and_tested(st, str(tmp_path))


def test_big_change_or_red_suite_keeps_the_full_check(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("x = 1\n")
    head = _repo(tmp_path)
    (tmp_path / "a.py").write_text("".join(f"v{i} = {i}\n" for i in range(200)))
    st = SimpleNamespace(head0=head, cwd=str(tmp_path), edits_made=1)
    _green_run(st, tmp_path)
    assert not _outcomes._small_and_tested(st, str(tmp_path))      # 200 lines: not small
    (tmp_path / "a.py").write_text("x = 2\n")
    _green_run(st, tmp_path)
    assert _outcomes._small_and_tested(st, str(tmp_path))
    st.last_full_green = False                                      # whole suite was red
    assert not _outcomes._small_and_tested(st, str(tmp_path))
    st.last_full_green = None
    monkeypatch.setenv("AIFORGE_CHAT_SMALL_SKIP", "0")
    assert not _outcomes._small_and_tested(st, str(tmp_path))


def test_a_piped_run_does_not_count(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    head = _repo(tmp_path)
    (tmp_path / "a.py").write_text("x = 2\n")
    st = SimpleNamespace(head0=head, cwd=str(tmp_path), edits_made=1)
    _outcomes._note_green_tests(
        st, "run_command", {"cmd": "python -m pytest tests -q | tail -3"},
        {"ok": True, "stdout": "4 passed in 0.1s\n"}, str(tmp_path))
    assert not _outcomes._small_and_tested(st, str(tmp_path))


def test_a_red_run_after_the_green_one_keeps_the_full_check(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    head = _repo(tmp_path)
    (tmp_path / "a.py").write_text("x = 2\n")
    st = SimpleNamespace(head0=head, cwd=str(tmp_path), edits_made=1)
    _green_run(st, tmp_path)
    assert _outcomes._small_and_tested(st, str(tmp_path))
    _outcomes._note_green_tests(
        st, "run_command", {"cmd": "python -m pytest tests/test_old.py -q"},
        {"ok": False, "stdout": "1 failed, 3 passed in 0.1s\n"}, str(tmp_path))
    assert not _outcomes._small_and_tested(st, str(tmp_path))


def test_more_than_three_files_or_new_files_count(tmp_path):
    for n in "abcd":
        (tmp_path / f"{n}.py").write_text("x = 1\n")
    head = _repo(tmp_path)
    for n in "abcd":
        (tmp_path / f"{n}.py").write_text("x = 2\n")
    st = SimpleNamespace(head0=head, cwd=str(tmp_path), edits_made=4)
    _green_run(st, tmp_path)
    assert not _outcomes._small_and_tested(st, str(tmp_path))      # 4 files
    for n in "bcd":
        (tmp_path / f"{n}.py").write_text("x = 1\n")
    (tmp_path / "new.py").write_text("".join(f"v{i} = {i}\n" for i in range(150)))
    _green_run(st, tmp_path)
    assert not _outcomes._small_and_tested(st, str(tmp_path))      # new file: 150 lines


def test_an_unrelated_green_run_does_not_count(tmp_path):
    (tmp_path / "parser.py").write_text("x = 1\n")
    head = _repo(tmp_path)
    (tmp_path / "parser.py").write_text("x = 2\n")
    st = SimpleNamespace(head0=head, cwd=str(tmp_path), edits_made=1)
    _outcomes._note_green_tests(
        st, "run_command", {"cmd": "python -m pytest tests/test_utils.py -q"},
        {"ok": True, "stdout": "4 passed in 0.1s\n"}, str(tmp_path))
    assert not _outcomes._small_and_tested(st, str(tmp_path))
    _outcomes._note_green_tests(
        st, "run_command", {"cmd": "cd x && python -m pytest tests/test_parser.py -q"},
        {"ok": True, "stdout": "4 passed in 0.1s\n"}, str(tmp_path))
    assert _outcomes._small_and_tested(st, str(tmp_path))


def test_view_counts_lines_like_read_lines():
    before = "a\x0cb\nc\nd\n"
    v = _edit_view.view(before, before.replace("d", "D"))
    assert v["start"] == 1 and v["text"].endswith("D\n")      # "a\x0cb" is ONE line
