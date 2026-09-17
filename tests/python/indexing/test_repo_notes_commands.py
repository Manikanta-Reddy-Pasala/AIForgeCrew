"""repo_notes._commands reads the repo's standards manifest.

It imported ``repo_standards`` from a package it had moved out of; the
best-effort ``except`` hid the ImportError, so every REPO_NOTES.md listed no
build or test commands at all.
"""
from __future__ import annotations

from aiforge_core.indexing import repo_notes as rn


def test_a_python_repo_gets_its_commands(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n")
    cmds = rn._commands("demo", str(tmp_path))
    assert set(cmds) == {"build", "compile", "test"}
    assert "pytest" in cmds["test"]


def test_the_commands_section_comes_before_the_long_sections():
    notes = rn.RepoNotes(repo="svc", worktree="/nowhere")
    notes.purpose = "Order service."
    notes.commands = {"build": "uv sync", "compile": "", "test": "python -m pytest -q"}
    notes.controllers = [{"file": "src/OrderController.java", "class_path": "/orders",
                          "endpoints": ["GET /orders/{id}"]}]
    body = rn._notes_body(notes)
    assert "- **test**: `python -m pytest -q`" in body
    assert "**compile**" not in body
    assert body.index("## Build / test commands") < body.index("## Controllers")


def test_a_broken_manifest_is_logged_not_raised(tmp_path, monkeypatch, caplog):
    from aiforge_core.config import repo_standards

    def boom(*_a, **_k):
        raise RuntimeError("bad manifest")
    monkeypatch.setattr(repo_standards, "get", boom)
    assert rn._commands("demo", str(tmp_path)) == {}
    assert "no commands for demo" in caplog.text
