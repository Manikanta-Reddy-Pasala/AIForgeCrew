from __future__ import annotations

import os

import pytest

from aiforge_core.runtime.visual import _captures


@pytest.fixture(autouse=True)
def _isolated_root(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))


def test_save_and_look_up():
    cid, path = _captures.save_capture(b"\x89PNG\r\n\x1a\nxx", "login")
    assert os.path.isfile(path)
    assert cid.startswith("login-")
    assert _captures.capture_path(cid) == path


def test_unknown_id_is_none():
    assert _captures.capture_path("nope") is None


@pytest.mark.parametrize("bad", ["../../etc/passwd", "a/b", "", "  ",
                                 ".hidden", "./x"])
def test_traversal_ids_rejected(bad, tmp_path):
    # The id becomes a filename; an id carrying a separator must never be
    # allowed to address a file outside the captures directory.
    assert _captures.capture_path(bad) is None


def test_label_is_sanitised():
    cid, path = _captures.save_capture(b"png", "../../evil name!")
    assert "/" not in cid
    assert os.path.dirname(path) == _captures.captures_dir()


def test_prune_keeps_the_newest(monkeypatch):
    monkeypatch.setattr(_captures, "_KEEP", 3)
    ids = [_captures.save_capture(b"png", f"s{i}")[0] for i in range(6)]
    remaining = [n for n in os.listdir(_captures.captures_dir())
                 if n.endswith(".png")]
    assert len(remaining) == 3
    assert _captures.capture_path(ids[-1]) is not None


def test_prune_never_drops_the_capture_just_taken(monkeypatch):
    # Captures written inside one filesystem timestamp tick all report the
    # SAME mtime; sorting on mtime alone then leaves the order to listdir,
    # and the newest could land in the delete slice — after its id had
    # already been handed to the caller.
    monkeypatch.setattr(_captures, "_KEEP", 3)
    monkeypatch.setattr(_captures.os.path, "getmtime", lambda _p: 1000.0)
    ids = [_captures.save_capture(b"png", f"s{i}")[0] for i in range(8)]
    assert _captures.capture_path(ids[-1]) is not None
    assert len([n for n in os.listdir(_captures.captures_dir())
                if n.endswith(".png")]) == 3


def test_capture_ids_sort_chronologically():
    # The filename is the tie-break, so it has to be monotonic: a
    # second-resolution stamp made every id in one second compare equal.
    a = _captures.save_capture(b"png", "ui")[0]
    b = _captures.save_capture(b"png", "ui")[0]
    assert a < b


def test_unwritable_store_degrades_instead_of_raising(monkeypatch):
    def _boom(*a, **kw):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("builtins.open", _boom)
    # A root-owned ~/.aiforge has bitten this project before: losing the
    # capture id must not cost the caller its whole result.
    assert _captures.save_capture(b"png", "ui") == (None, None)
