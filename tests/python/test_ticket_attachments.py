"""Ticket attachment persist + remove helpers (added 2026-05-31).

Filesystem-only — no Postgres needed. Exercises the add/remove file
helpers that back PATCH /api/tickets/{id} body+attachment editing.
"""
from __future__ import annotations

import base64

import pytest

from aiforge_core.api import api as api_mod


@pytest.fixture()
def repo_root(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_REPO_ROOT", str(tmp_path))
    return tmp_path


def _b64(s: bytes) -> str:
    return base64.b64encode(s).decode()


def test_persist_writes_files(repo_root):
    files = [
        api_mod.AttachedFile(name="a.txt", size=3, content_b64=_b64(b"abc")),
        api_mod.AttachedFile(name="b.png", size=2, content_b64=_b64(b"hi")),
    ]
    meta = api_mod._persist_ticket_attachments("ONE-1", files)
    assert {m["name"] for m in meta} == {"a.txt", "b.png"}
    d = repo_root / ".aiforge" / "ticket-files" / "ONE-1"
    assert (d / "a.txt").read_bytes() == b"abc"
    assert (d / "b.png").read_bytes() == b"hi"


def test_remove_deletes_files(repo_root):
    files = [
        api_mod.AttachedFile(name="a.txt", size=3, content_b64=_b64(b"abc")),
        api_mod.AttachedFile(name="b.png", size=2, content_b64=_b64(b"hi")),
    ]
    api_mod._persist_ticket_attachments("ONE-1", files)
    removed = api_mod._remove_ticket_attachments("ONE-1", ["a.txt"])
    assert removed == ["a.txt"]
    d = repo_root / ".aiforge" / "ticket-files" / "ONE-1"
    assert not (d / "a.txt").exists()
    assert (d / "b.png").exists()


def test_remove_missing_is_noop(repo_root):
    removed = api_mod._remove_ticket_attachments("ONE-1", ["ghost.txt"])
    assert removed == []


def test_remove_strips_path_traversal(repo_root):
    # A malicious name must not escape the per-ticket dir.
    sentinel = repo_root / "secret.txt"
    sentinel.write_text("keep me")
    api_mod._remove_ticket_attachments("ONE-1", ["../../secret.txt"])
    assert sentinel.exists()
