"""The block of measured facts under a final answer."""
import subprocess

import pytest

from aiforge_core.runtime import action_log
from aiforge_core.runtime import turn_facts_line as facts

GIT = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "calc.py").write_text("x = 1\n")
    for cmd in (["git", "init", "-q", "-b", "main"], ["git", "add", "-A"],
                GIT + ["commit", "-q", "-m", "init"]):
        subprocess.run(cmd, cwd=tmp_path, check=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path,
                          capture_output=True, text=True).stdout.strip()
    return tmp_path, head


@pytest.fixture
def log(monkeypatch):
    """Feed the turn's steps, and the steps of the chat's earlier turns."""
    state = {"live": [], "earlier": []}
    monkeypatch.setattr(action_log, "live_steps", lambda _sid: state["live"])
    monkeypatch.setattr(action_log, "_stored_steps", lambda _sid: state["earlier"])
    monkeypatch.setenv("AIFORGE_CHAT_FACTS_LINE", "1")
    return state


def _step(name, ok=True, **args):
    res = {"ok": ok} if ok else {"ok": False, "error": "exit 1: 2 failed"}
    return {"type": "tool", "name": name, "args": args, "result": res}


def test_a_turn_that_changed_and_committed_says_so(repo, log):
    cwd, head = repo
    (cwd / "calc.py").write_text("x = 2\n")
    (cwd / "new.py").write_text("y = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=cwd, check=True)
    subprocess.run(GIT + ["commit", "-q", "-m", "c"], cwd=cwd, check=True)
    log["live"] = [_step("file_write", path="calc.py"),
                   _step("run_command", cmd="pytest")]
    out = facts.suffix(1, str(cwd), head)
    assert facts.HEAD in out
    assert "files changed in this turn: `calc.py`, `new.py`" in out
    assert "1 commit made" in out and "actions run: 2 worked" in out


def test_a_failed_command_is_named(repo, log):
    cwd, head = repo
    (cwd / "calc.py").write_text("x = 3\n")
    log["live"] = [_step("file_write", path="calc.py"),
                   _step("run_command", ok=False, cmd="pytest -q")]
    out = facts.suffix(1, str(cwd), head)
    assert "1 worked, 1 failed (last:" in out and "pytest -q" in out


def test_a_turn_that_changed_nothing_names_the_earlier_files(repo, log):
    cwd, head = repo
    log["earlier"] = [_step("file_write", path=f"{cwd}/calc.py"),
                      _step("file_write", ok=False, path="bad.py"),
                      _step("file_read", path="other.py")]
    out = facts.suffix(1, str(cwd), head)
    assert "no file changed in this turn" in out
    assert "changed earlier in this chat: `calc.py`" in out and "bad.py" not in out
    assert "not pushed (the repository has no remote)" in out


def test_the_push_state_is_read_from_git(repo, log, tmp_path_factory):
    cwd, head = repo
    remote = tmp_path_factory.mktemp("remote")
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=cwd, check=True)
    log["earlier"] = [_step("file_write", path="calc.py")]
    assert "not pushed (the branch has no upstream)" in facts.suffix(1, str(cwd), head)
    subprocess.run(["git", "push", "-q", "-u", "origin", "main"], cwd=cwd, check=True)
    assert "pushed to `origin/main`" in facts.suffix(1, str(cwd), head)
    (cwd / "calc.py").write_text("x = 5\n")
    subprocess.run(GIT + ["commit", "-q", "-am", "c2"], cwd=cwd, check=True)
    assert "1 commit(s) not pushed to `origin/main`" in facts.suffix(1, str(cwd), head)


def test_a_plain_answer_in_a_chat_that_never_wrote_gets_no_block(repo, log):
    cwd, head = repo
    assert facts.suffix(1, str(cwd), head) == ""
    log["live"] = [_step("file_read", path="calc.py")]
    assert facts.suffix(1, str(cwd), head) == ""


def test_no_session_or_switched_off_gives_nothing(repo, log, monkeypatch):
    cwd, head = repo
    (cwd / "calc.py").write_text("x = 9\n")
    assert facts.suffix(None, str(cwd), head) == ""
    monkeypatch.setenv("AIFORGE_CHAT_FACTS_LINE", "0")
    assert facts.suffix(1, str(cwd), head) == ""


def test_without_git_the_actions_are_still_shown(tmp_path, log):
    log["live"] = [_step("run_command", cmd="ls")]
    out = facts.suffix(1, str(tmp_path), None)
    assert "actions run: 1 worked" in out and "file changed" not in out


def test_a_block_the_model_copied_is_removed():
    text = f"Done.\n\n---\n_{facts.HEAD}:_ files changed in this turn: `a.py`."
    assert facts.strip_copied(text) == "Done."
    assert facts.strip_copied("Done.") == "Done."


def test_many_files_are_capped(repo, log):
    cwd, head = repo
    for i in range(9):
        (cwd / f"f{i}.py").write_text("1\n")
    out = facts.suffix(1, str(cwd), head)
    assert "(+3 more)" in out


def test_files_the_harness_wrote_are_not_the_turns_work(repo, log):
    cwd, head = repo
    (cwd / ".codegraph").mkdir()
    (cwd / ".codegraph" / ".gitignore").write_text("*\n")
    assert facts.suffix(1, str(cwd), head) == ""
