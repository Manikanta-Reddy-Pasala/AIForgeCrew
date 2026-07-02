"""Custom slash commands — user-defined reusable prompt templates.

Mirrors the skills loader's file-discovery: markdown files under a global
dir + repo-local dirs, repo overrides global. ``/name args`` invocations
expand to the file body with ``$ARGUMENTS`` / ``$N`` substituted; a message
that is not a known ``/command`` passes through unchanged (returns ``None``).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from aiforge_core.runtime import commands


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    # Point the GLOBAL dir at an empty tmp by default + neutralise workspace
    # env so _repo_root resolves to the cwd we pass explicitly.
    monkeypatch.setenv("AIFORGE_COMMANDS_DIR", str(tmp_path / "global"))
    for k in ("AIFORGE_WORKSPACE_DIR", "AIFORGE_REPO_ROOT"):
        monkeypatch.delenv(k, raising=False)


def test_load_discovers_repo_local_command(tmp_path):
    repo = tmp_path / "repo"
    _write(repo / ".aiforge" / "commands" / "deploy.md", "Deploy to $ARGUMENTS.")
    cmds = commands.load(str(repo))
    assert "deploy" in cmds
    assert cmds["deploy"].name == "deploy"
    assert "Deploy to" in cmds["deploy"].body


def test_expand_substitutes_arguments_and_positionals(tmp_path):
    repo = tmp_path / "repo"
    _write(repo / ".aiforge" / "commands" / "deploy.md",
           "Deploy to $ARGUMENTS. First=$1 second=$2 third=$3")
    out = commands.expand("/deploy staging now", str(repo))
    assert out is not None
    assert "$ARGUMENTS" not in out
    assert "Deploy to staging now." in out
    assert "First=staging second=now" in out
    # unmatched positional ($3) left as-is
    assert "third=$3" in out


def test_repo_overrides_global_on_name_clash(tmp_path, monkeypatch):
    gdir = tmp_path / "global"
    _write(gdir / "deploy.md", "GLOBAL body $ARGUMENTS")
    monkeypatch.setenv("AIFORGE_COMMANDS_DIR", str(gdir))
    repo = tmp_path / "repo"
    _write(repo / ".aiforge" / "commands" / "deploy.md", "REPO body $ARGUMENTS")
    out = commands.expand("/deploy here", str(repo))
    assert out == "REPO body here"


def test_non_command_message_returns_none(tmp_path):
    assert commands.expand("hello world", str(tmp_path)) is None
    # a bare slash that is not a /name token must pass through too
    assert commands.expand("/ 2 + 2", str(tmp_path)) is None


def test_unknown_command_passes_through(tmp_path):
    repo = tmp_path / "repo"
    _write(repo / ".aiforge" / "commands" / "deploy.md", "Deploy $ARGUMENTS")
    assert commands.expand("/unknown x", str(repo)) is None


def test_frontmatter_description_and_body_template(tmp_path):
    repo = tmp_path / "repo"
    _write(repo / ".aiforge" / "commands" / "review.md",
           "---\ndescription: Review a PR thoroughly\n---\nReview PR $ARGUMENTS carefully.")
    cmds = commands.load(str(repo))
    assert cmds["review"].description == "Review a PR thoroughly"
    # body is the text AFTER the frontmatter, not the frontmatter itself
    assert "description:" not in cmds["review"].body
    out = commands.expand("/review 42", str(repo))
    assert out == "Review PR 42 carefully."


def test_claude_commands_dir_also_discovered(tmp_path):
    repo = tmp_path / "repo"
    _write(repo / ".claude" / "commands" / "ship.md", "Ship it: $ARGUMENTS")
    out = commands.expand("/ship now", str(repo))
    assert out == "Ship it: now"


def test_list_commands_shape(tmp_path):
    repo = tmp_path / "repo"
    _write(repo / ".aiforge" / "commands" / "deploy.md", "Deploy $ARGUMENTS")
    listed = commands.list_commands(str(repo))
    assert any(c["name"] == "deploy" for c in listed)
    entry = next(c for c in listed if c["name"] == "deploy")
    assert set(entry) >= {"name", "description", "source"}


def test_builtin_help_lists_commands(tmp_path):
    repo = tmp_path / "repo"
    _write(repo / ".aiforge" / "commands" / "deploy.md", "Deploy $ARGUMENTS")
    out = commands.expand("/help", str(repo))
    assert out is not None
    assert "/deploy" in out


def test_expand_no_args_leaves_positional(tmp_path):
    repo = tmp_path / "repo"
    _write(repo / ".aiforge" / "commands" / "deploy.md", "Deploy $ARGUMENTS to $1")
    out = commands.expand("/deploy", str(repo))
    assert out == "Deploy  to $1"
