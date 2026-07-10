"""REPO_NOTES.md is wired into the context bundle: its structural knowledge
(controllers/services/event surface) reaches the window, with the OKR envelope
metadata (Objective/title/sentinel) stripped.
"""
from __future__ import annotations

import os
import subprocess

import pytest


def _repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    d = tmp_path / ".aiforge"
    d.mkdir()
    from aiforge_core.indexing.repo_notes import render_markdown, RepoNotes
    n = RepoNotes(repo="svc", worktree=str(tmp_path))
    n.purpose = "Order service."
    n.controllers = [{"file": "src/OrderController.java", "class_path": "/orders",
                      "endpoints": ["GET /orders/{id}"]}]
    n.nats_subjects = {"publish": ["order.created"], "subscribe": []}
    (d / "REPO_NOTES.md").write_text(render_markdown(n), encoding="utf-8")
    return str(tmp_path)


def test_repo_notes_loads_knowledge(tmp_path):
    from aiforge_core.runtime import context_bundle as cb
    md = cb._repo_notes(_repo(tmp_path))
    assert md.startswith("REPO STRUCTURE")
    assert "OrderController.java" in md          # code link
    assert "order.created" in md                 # service link
    assert "1 controllers" in md                 # Key Results scan count
    assert "## Objective" not in md              # envelope metadata stripped
    assert "aiforge:body" not in md


def test_repo_notes_found_via_git_toplevel(tmp_path):
    # cwd is a SUBDIR of the repo; notes at repo root must still be found
    root = _repo(tmp_path)
    sub = os.path.join(root, "src", "main")
    os.makedirs(sub)
    from aiforge_core.runtime import context_bundle as cb
    assert "OrderController.java" in cb._repo_notes(sub)


def test_repo_notes_empty_when_absent(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    from aiforge_core.runtime import context_bundle as cb
    assert cb._repo_notes(str(tmp_path)) == ""


def test_bundle_includes_repo_notes(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    from aiforge_core.runtime import context_bundle as cb
    b = cb.build_bundle(root, "add an endpoint", want_repo_map=False)
    assert "OrderController.java" in b.repo_notes_md
    assert any("REPO STRUCTURE" in blk for blk in b.blocks())


def test_repo_notes_skipped_in_cave(tmp_path):
    root = _repo(tmp_path)
    from aiforge_core.runtime import context_bundle as cb
    b = cb.build_bundle(root, "x", cave=True, want_repo_map=False)
    assert b.repo_notes_md == ""             # cave = leanest context
