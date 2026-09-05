"""safe_paths: turning a path that arrived from outside into one we may walk.

The property under test is not "the value was checked" — ``realpath`` plus
``isdir`` already proved that — but "the value never became the argument of a
filesystem call". ``safe_dir`` answers with a string re-spelled entirely from
``os.scandir`` output, so the caller's bytes only ever appear on the right of
an ``==``. That is what a taint analyser can see, and it is also what survives
the next edit: a caller that stops re-checking is still not joining request
bytes into a path.

So these pin both halves — the same directory comes back, and a directory that
is not exactly there comes back as "" rather than as the caller's spelling.
"""
from __future__ import annotations

import os

import pytest

from aiforge_core.config import safe_paths
from aiforge_core.config.safe_paths import safe_dir, safe_segment


def test_a_real_directory_answers_with_its_resolved_self(tmp_path):
    d = tmp_path / "repo"
    d.mkdir()
    assert safe_dir(str(d)) == os.path.realpath(str(d))


def test_dot_segments_are_resolved_before_the_answer(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    assert safe_dir(str(tmp_path / "a" / ".." / "b")) \
        == os.path.realpath(str(tmp_path / "b"))


def test_a_symlink_answers_with_its_target(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    assert safe_dir(str(link)) == os.path.realpath(str(real))


@pytest.mark.parametrize("value", ["", "   ", None, "/no\x00pe"])
def test_a_value_that_is_not_a_path_is_refused(value):
    assert safe_dir(value) == ""


def test_a_file_is_not_a_directory(tmp_path):
    f = tmp_path / "pom.xml"
    f.write_text("<project/>")
    assert safe_dir(str(f)) == ""


def test_a_directory_that_is_not_there_is_refused(tmp_path):
    assert safe_dir(str(tmp_path / "gone")) == ""


def test_a_path_the_os_cannot_resolve_is_refused(tmp_path, monkeypatch):
    def _boom(_p):
        raise ValueError("embedded null byte")
    monkeypatch.setattr(os.path, "realpath", _boom)
    assert safe_dir(str(tmp_path)) == ""


def test_a_path_outside_the_permitted_roots_is_refused(tmp_path):
    inside = tmp_path / "root" / "repo"
    inside.mkdir(parents=True)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    root = str(tmp_path / "root")
    assert safe_dir(str(inside), roots=[root]) == os.path.realpath(str(inside))
    assert safe_dir(str(outside), roots=[root]) == ""


def test_a_blank_root_does_not_permit_everything(tmp_path):
    d = tmp_path / "repo"
    d.mkdir()
    assert safe_dir(str(d), roots=["", "   "]) == ""


# ─── the re-spelling itself ────────────────────────────────────────────


def test_the_answer_is_spelled_from_the_filesystem_not_the_caller(tmp_path):
    """The caller's casing does not survive; the directory entry's does.

    On a case-insensitive filesystem ``realpath`` leaves the caller's spelling
    alone, so this is the branch that keeps macOS and Windows working — and it
    is also the clearest demonstration that the returned string is built from
    scandir output rather than from the argument.
    """
    (tmp_path / "Repo").mkdir()
    answered = safe_paths._respelled(str(tmp_path / "repo"))
    assert answered == os.path.join(str(tmp_path), "Repo")


def test_an_exact_match_wins_over_a_differently_cased_one(tmp_path):
    (tmp_path / "Repo").mkdir()
    (tmp_path / "repo").mkdir()
    assert safe_paths._respelled(str(tmp_path / "repo")) \
        == os.path.join(str(tmp_path), "repo")


def test_a_component_that_vanished_is_refused(tmp_path):
    assert safe_paths._respelled(str(tmp_path / "gone" / "deeper")) == ""


def test_a_component_that_is_a_file_is_refused(tmp_path):
    f = tmp_path / "notadir"
    f.write_text("x")
    assert safe_paths._respelled(str(f / "child")) == ""
    assert safe_paths._respelled(str(f)) == "", "the last component too"


def test_a_drive_letter_is_rebuilt_from_the_literal_alphabet():
    """The Windows prefix, on the box where these actually run.

    "" means posix (no drive to rebuild); None means the prefix was not a
    drive letter at all, which is the caller's bytes and so cannot be part of
    an answer.
    """
    assert safe_paths._drive_prefix("c:") == "C:"
    assert safe_paths._drive_prefix("") == ""
    assert safe_paths._drive_prefix("1:") is None


def test_a_drive_letter_that_is_not_one_is_refused(tmp_path, monkeypatch):
    """The Windows branch, exercised where the tests actually run.

    The drive prefix is rebuilt from a literal alphabet for the same reason
    every other component is rebuilt from scandir: nothing the caller typed is
    allowed into the string. A prefix that matches no letter has nothing to
    rebuild it from.
    """
    monkeypatch.setattr(os.path, "splitdrive",
                        lambda p: ("1:", str(tmp_path)))
    assert safe_paths._respelled("1:" + str(tmp_path)) == ""


# ─── one segment, or nothing ───────────────────────────────────────────


def test_a_plain_identifier_is_a_segment():
    assert safe_segment("ONE-1") == "ONE-1"
    assert safe_segment("  ONE-1  ") == "ONE-1"


@pytest.mark.parametrize("value", ["", None, ".", "..", "../ONE-1", "a/b",
                                   "a\\b", "ONE\x00-1"])
def test_anything_that_is_not_one_segment_is_refused(value):
    assert safe_segment(value) == "", "refused, not sanitised into a segment"
