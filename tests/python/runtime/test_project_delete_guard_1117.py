"""Deleting a file the project already tracks needs the user."""
import os
import subprocess
from types import SimpleNamespace

import pytest

from aiforge_core.runtime.chat_agent._turn import _project_delete_guard as G


@pytest.fixture
def repo(tmp_path):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "data.txt").write_text("x\n" * 50)
    for c in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "s"]):
        subprocess.run(["git", "-C", str(tmp_path), *c], check=True, capture_output=True, env=env)
    (tmp_path / "scratch.txt").write_text("new\n")            # the turn's own, untracked
    return tmp_path


def test_tracked_file_delete_is_refused_even_with_confirm(repo, monkeypatch):
    monkeypatch.delenv("AIFORGE_CHAT_KEEP_PROJECT_FILES", raising=False)
    st = SimpleNamespace(cwd=str(repo), goal="Fix the width of halfwidth marks.")
    for args in ({"cmd": f"cd {repo} && rm tests/data.txt && python3 gen.py"},
                 {"cmd": "rm -f tests/data.txt", "confirm_delete": True},
                 {"cmd": "rm -rf tests"}):
        r = G.refusal(st, "run_command", args, str(repo))
        assert r and r["blocked"] == "delete_project_file", args


def test_untracked_or_asked_deletes_pass(repo, monkeypatch):
    monkeypatch.delenv("AIFORGE_CHAT_KEEP_PROJECT_FILES", raising=False)
    st = SimpleNamespace(cwd=str(repo), goal="Fix the width of halfwidth marks.")
    assert G.refusal(st, "run_command", {"cmd": "rm scratch.txt"}, str(repo)) is None
    asked = SimpleNamespace(cwd=str(repo), goal="Remove the old data file tests/data.txt.")
    assert G.refusal(asked, "run_command", {"cmd": "rm tests/data.txt"}, str(repo)) is None
    monkeypatch.setenv("AIFORGE_CHAT_KEEP_PROJECT_FILES", "0")
    assert G.refusal(st, "run_command", {"cmd": "rm tests/data.txt"}, str(repo)) is None
