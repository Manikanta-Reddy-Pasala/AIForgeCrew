"""aiforge-tool CLI — read-only dispatch into the chat tool registry so job/
workflow scripts consume configured integrations instead of raw REST."""
from __future__ import annotations

import json

import pytest

from aiforge_core.runtime import tool_cli


@pytest.fixture
def no_writes(monkeypatch):
    monkeypatch.delenv("AIFORGE_TOOL_CLI_ALLOW_WRITES", raising=False)


def test_unknown_tool_refused(no_writes, capsys):
    assert tool_cli.main(["definitely_not_a_tool"]) == 3
    out = json.loads(capsys.readouterr().out)
    assert not out["ok"] and "unknown tool" in out["error"]


def test_write_tool_refused_headless(no_writes, capsys):
    # file_write is a registered WRITE tool — must refuse without the env.
    assert tool_cli.main(["file_write", "{}"]) == 3
    out = json.loads(capsys.readouterr().out)
    assert "WRITE tool" in out["error"]


def test_readonly_tool_dispatches(no_writes, capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "hello.txt").write_text("hi\n")
    rc = tool_cli.main(["list_dir", "{}"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0, out
    assert "hello.txt" in json.dumps(out)


def test_bad_json_args(no_writes, capsys):
    assert tool_cli.main(["list_dir", "{not json"]) == 3
    out = json.loads(capsys.readouterr().out)
    assert "bad JSON args" in out["error"]


def test_list_shows_only_readonly(no_writes, capsys):
    assert tool_cli.main(["--list"]) == 0
    names = capsys.readouterr().out.split()
    assert "jira_search" in names or "jira_read" in names
    assert "file_write" not in names