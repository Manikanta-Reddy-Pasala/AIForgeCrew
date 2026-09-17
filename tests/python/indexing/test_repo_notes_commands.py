"""repo_notes._commands reads the repo's standards manifest.

It imported ``repo_standards`` from a package it had moved out of; the
best-effort ``except`` hid the ImportError, so every REPO_NOTES.md listed no
build or test commands at all.
"""
from __future__ import annotations

from aiforge_core.indexing import repo_notes as rn


def test_a_python_repo_gets_its_commands(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n")
    (tmp_path / "app.py").write_text("print('hi')\n")
    cmds = rn._commands("demo", str(tmp_path))
    assert set(cmds) == {"build", "compile", "test", "lint", "format"}
    assert "pytest" in cmds["test"]


def test_the_commands_match_the_standards_manifest(tmp_path):
    from aiforge_core.config import repo_standards
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n")
    std = repo_standards.get("demo", worktree=str(tmp_path))
    cmds = rn._commands("demo", str(tmp_path))
    assert cmds["build"] == std.build_cmd
    assert cmds["lint"] == std.lint_cmd
