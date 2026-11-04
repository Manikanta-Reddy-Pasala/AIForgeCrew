"""click af9 ran `rm -f … src/click/core.py …` 20 minutes in and restored the
files from git: every edit of the run gone. The command is refused."""
from __future__ import annotations

import os
import subprocess
from types import SimpleNamespace

import pytest

from aiforge_core.runtime.chat_agent._turn import _discard_guard as G


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "src").mkdir()
    for f in ("core.py", "types.py", "other.py"):
        (tmp_path / "src" / f).write_text("x = 1\n")
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "PATH": os.environ["PATH"]}
    for c in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "s"]):
        subprocess.run(["git", "-C", str(tmp_path), *c], check=True, capture_output=True, env=env)
    (tmp_path / "src" / "new_mod.py").write_text("y = 2\n")
    (tmp_path / "debug.py").write_text("print(1)\n")
    edited = [str(tmp_path / "src" / "core.py"), str(tmp_path / "src" / "new_mod.py"),
              str(tmp_path / "debug.py")]
    return tmp_path, edited


def _hits(repo, cmd, cwd=None):
    root, edited = repo
    return [os.path.relpath(p, root) for p in G.hits(cmd, str(cwd or root), edited)]


@pytest.mark.parametrize("cmd", [
    "cd {r} && rm -f tests/t.py src/core.py src/types.py",
    "cd {r}\nrm -f src/core.py",
    "sleep 1 & rm src/core.py",
    "rm -f src/*.py",
    "(cd src && rm core.py)",
    "git -C {r} checkout -- src/core.py",
    "git checkout HEAD~1 src/core.py",
    "git checkout .",
    "git restore src",
    "git stash",
    "git stash push -- src",
    "git reset --hard HEAD",
    "git rm src/core.py",
    "bash -c 'rm src/core.py'",
    "env A=1 rm src/core.py",
    "rm -f \\\n  src/core.py src/types.py",
    "cd tests 2>/dev/null; cd {r}/src && git reset --hard",
    "bash -lc 'rm src/core.py'",
    "git checkout -f main",
    "git show HEAD:src/core.py > src/core.py",
    "cp /tmp/orig.py src/core.py",
    "git clean --force -d",
    "rm -rf src",
])
def test_commands_that_discard_the_runs_edits_are_caught(repo, cmd):
    assert _hits(repo, cmd.format(r=repo[0])), cmd


@pytest.mark.parametrize("cmd", [
    "rm -f src/other.py", "rm src/core.py.bak", "rm debug.py", "git stash list",
    "git stash pop", "git stash push -- src/other.py", "git status",
    "git diff src/core.py", "git checkout main", "git checkout -b feature",
    "git restore --staged src/core.py", "git reset --soft HEAD~1", "git clean -fn",
    "git clean -fX", "ls src && cat src/core.py", "ssh host 'rm src/core.py'",
])
def test_harmless_commands_pass(repo, cmd):
    assert _hits(repo, cmd) == [], cmd


def test_git_clean_hits_only_the_untracked_new_files(repo):
    assert sorted(_hits(repo, "git clean -fd")) == ["debug.py", "src/new_mod.py"]


def test_reset_and_plain_stash_leave_untracked_work_alone(repo):
    assert _hits(repo, "git reset --hard") == ["src/core.py"]
    assert _hits(repo, "git stash") == ["src/core.py"]
    assert "src/new_mod.py" in _hits(repo, "git stash -u")


def test_outside_a_git_repo_every_edited_file_is_protected(tmp_path):
    (tmp_path / "app.py").write_text("x\n")
    assert G.hits("rm app.py", str(tmp_path), [str(tmp_path / "app.py")])


def test_a_deleted_file_no_longer_protects_anything(repo):
    root, edited = repo
    (root / "src" / "core.py").unlink()
    (root / "src" / "new_mod.py").unlink()
    assert G.hits("git stash -u", str(root), edited) == [str(root / "debug.py")]


def test_the_refusal_names_the_files_and_takes_the_override(repo, monkeypatch):
    root, edited = repo
    from aiforge_core.runtime import git_target
    monkeypatch.setattr(git_target, "command_base", lambda cwd: str(root))
    st = SimpleNamespace(file_hashes=dict.fromkeys(edited, "h"), goal="")
    r = G.refusal(st, "run_command", {"cmd": "rm src/core.py"}, str(root))
    assert r["error"] == "would_discard_own_edits" and r["files"] == ["src/core.py"]
    for v in (True, "true", "1"):
        assert G.refusal(st, "run_command", {"cmd": "rm src/core.py", "discard": v},
                         str(root)) is None


def test_removing_a_new_unnamed_probe_file_by_name_is_cleanup(repo):
    root, edited = repo
    (root / "test_completion_item.py").write_text("def test_x(): pass\n")
    edited = edited + [str(root / "test_completion_item.py")]
    assert G.hits("rm -f test_completion_item.py", str(root), edited, goal="build it") == []
    # a new file the request names stays protected, so do tracked files and globs
    assert G.hits("rm src/new_mod.py", str(root), edited, goal="add `src/new_mod.py`")
    assert G.hits("rm src/core.py", str(root), edited, goal="")
    assert G.hits("rm -f *.py src/*.py", str(root), edited, goal="")


def test_the_same_refused_command_sent_again_goes_through(repo, monkeypatch):
    root, edited = repo
    from aiforge_core.runtime import git_target
    monkeypatch.setattr(git_target, "command_base", lambda cwd: str(root))
    st = SimpleNamespace(file_hashes=dict.fromkeys(edited, "h"), goal="")
    assert G.refusal(st, "run_command", {"cmd": "rm src/core.py"}, str(root))
    assert G.refusal(st, "run_command", {"cmd": "rm  src/core.py"}, str(root)) is None
