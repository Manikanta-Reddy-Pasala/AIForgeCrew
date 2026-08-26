"""Sync can never write into an authored ``okf/`` tree.

The client's own notes and the admin's own notes are the two things this feature
must not be able to corrupt, so the rule is ENFORCED at the write rather than
merely preferred at the routing decision (``paths.target_for``). These tests
force the routing to be wrong on purpose: a second check that only ever runs
after a correct decision proves nothing.
"""
from __future__ import annotations

import hashlib

import pytest

from aiforge_core.memory.sync import _io, apply, inbox


def _node_bytes(origin: str, key: str, rev: int = 1) -> bytes:
    return (f"---\norigin: {origin}\nkey: {key}\nrev: {rev}\n---\n\n"
            f"a note about `x/y.py` and `run_once()`\n").encode()


def _entry(origin: str, key: str, body: bytes, path: str) -> dict:
    return {"kind": "B", "origin": origin, "key": key, "rev": 1,
            "hash": hashlib.sha256(body).hexdigest(), "path": path}


@pytest.fixture()
def tree(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    monkeypatch.setenv("AIFORGE_PEER_ID", "me")
    root = _io.root()
    (root / "okf").mkdir(parents=True, exist_ok=True)
    return root


def test_an_entry_aimed_at_okf_is_refused_not_written(tree, monkeypatch):
    body = _node_bytes("ms", "O-01")
    entry = _entry("ms", "O-01", body, "okf/O-01.md")
    monkeypatch.setattr("aiforge_core.memory.sync.paths.target_for",
                        lambda e: tree / "okf" / "O-01.md")

    assert apply.apply_blob(entry, body, peer_id="ms") is False
    assert not (tree / "okf" / "O-01.md").exists()


def test_a_pushed_entry_aimed_at_okf_is_refused(tree, monkeypatch):
    body = _node_bytes("ms", "O-02")
    entry = _entry("ms", "O-02", body, "okf/O-02.md")
    monkeypatch.setattr("aiforge_core.memory.sync.paths.target_for",
                        lambda e: tree / "okf" / "O-02.md")

    assert inbox.accept("ms", entry, body) is False
    assert not (tree / "okf" / "O-02.md").exists()


def test_a_refusal_costs_one_record_not_the_cycle(tree, monkeypatch):
    """The guard must behave like every other per-record refusal in the applier:
    the entry is dropped and the loop keeps going."""
    from aiforge_core.memory.sync import loop

    body = _node_bytes("ms", "O-03")
    entry = _entry("ms", "O-03", body, "okf/O-03.md")
    monkeypatch.setattr("aiforge_core.memory.sync.paths.target_for",
                        lambda e: tree / "okf" / "O-03.md")

    assert loop._apply_one(entry, body, "ms", apply) is False


def test_an_existing_authored_note_is_left_byte_for_byte(tree, monkeypatch):
    note = tree / "okf" / "O-04.md"
    note.write_bytes(_node_bytes("me", "O-04"))
    before = note.read_bytes(), note.stat().st_mtime_ns

    body = _node_bytes("ms", "O-04", rev=99)
    entry = _entry("ms", "O-04", body, "okf/O-04.md")
    monkeypatch.setattr("aiforge_core.memory.sync.paths.target_for", lambda e: note)

    assert apply.apply_blob(entry, body, peer_id="ms") is False
    assert (note.read_bytes(), note.stat().st_mtime_ns) == before


def test_a_tombstone_below_okf_is_still_allowed(tree, monkeypatch):
    """okf/.tomb/ is how a deletion propagates at all — the guard must not
    close the one legitimate network-driven write below okf/."""
    _io.assert_not_ours(tree / "okf" / ".tomb" / "ms" / "O-05.json")
