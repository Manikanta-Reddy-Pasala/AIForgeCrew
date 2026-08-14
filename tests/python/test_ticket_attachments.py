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
    # _ticket_files_base() resolves AIFORGE_TICKET_FILES_DIR, then
    # AIFORGE_CONFIG_DIR, then AIFORGE_REPO_ROOT. This test covers the
    # repo-root branch, so the two higher-precedence vars must be absent —
    # otherwise it silently exercises a different branch (and, before the
    # suite was isolated, wrote into the operator's real ~/.aiforge).
    monkeypatch.delenv("AIFORGE_TICKET_FILES_DIR", raising=False)
    monkeypatch.delenv("AIFORGE_CONFIG_DIR", raising=False)
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


# ── /files/{id}/{name} serving route (ONE-174 attachment-404 fix) ──────────


def test_serve_file_from_abs_path_outside_mount(tmp_path, monkeypatch):
    # The 404 scenario: the runner rebound AIFORGE_REPO_ROOT per ticket, so the
    # file was written to a per-ticket worktree the boot-time mount root does
    # NOT cover. The ticket's metadata.abs_path records the real location; the
    # route must serve from there.
    scattered = tmp_path / "worktrees" / "Scheduler" / ".aiforge" / "ticket-files" / "ONE-9"
    scattered.mkdir(parents=True)
    (scattered / "img.png").write_bytes(b"PNGDATA")
    monkeypatch.setattr(
        api_mod.tickets_mod, "get_enriched",
        lambda ident: {"metadata": {"attached_files": [
            {"name": "img.png", "abs_path": str(scattered / "img.png")}]}}
        if ident == "ONE-9" else None,
    )
    resp = api_mod.serve_ticket_file("ONE-9", "img.png")
    assert resp.path == str(scattered / "img.png")


def test_serve_file_404_when_missing(tmp_path, monkeypatch):
    # Empty base dir + no ticket metadata → 404, not a 500.
    monkeypatch.setenv("AIFORGE_TICKET_FILES_DIR", str(tmp_path / "empty"))
    monkeypatch.setattr(api_mod.tickets_mod, "get_enriched", lambda ident: None)
    with pytest.raises(api_mod.HTTPException) as exc:
        api_mod.serve_ticket_file("ONE-X", "nope.png")
    assert exc.value.status_code == 404


def test_serve_file_strips_path_traversal(tmp_path, monkeypatch):
    # A traversal name must be reduced to its basename and never escape the base.
    secret = tmp_path / "secret.txt"
    secret.write_text("keep me")
    monkeypatch.setenv("AIFORGE_TICKET_FILES_DIR", str(tmp_path))
    monkeypatch.setattr(api_mod.tickets_mod, "get_enriched", lambda ident: None)
    with pytest.raises(api_mod.HTTPException):
        api_mod.serve_ticket_file("ONE-1", "../secret.txt")
    assert secret.exists()
