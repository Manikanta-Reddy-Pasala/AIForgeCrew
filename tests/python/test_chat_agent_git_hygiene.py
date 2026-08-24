import json

import pytest

from aiforge_core.runtime import chat_agent as ca


def _scripted(outputs):
    """Return a complete_fn that yields the given outputs in order."""
    seq = list(outputs)

    def _fn(role, messages, **kw):
        return seq.pop(0)
    return _fn


def _collect(gen):
    return list(gen)


# ── commit hygiene — REFUSE blanket git stages (no rewrite/baseline) ──────────


def test_is_blanket_git_refuses_blanket_forms():
    refuse = [
        "git add -A", "git add .", "git add --all", "git add -A .",
        "git add -- .", 'git commit -am "x"', "git commit -a",
        'git commit --all -m "x"', "git commit -a -m msg",
        "(git add -A)", "(git add -A && git commit)",
        "(git add -A && git commit -am x)",
        "{ git add . ; git commit -m y ; }",
        "sudo git add -A", "FOO=bar git add -A", "git -C foo add -A",
        "git add -A && git commit && git push", "cd sub && git add -A",
    ]
    for c in refuse:
        assert ca._is_blanket_git(c) is True, f"should refuse: {c!r}"


def test_is_blanket_git_allows_targeted_and_quoted():
    allow = [
        "git add foo.py bar.py", "git commit -m x",
        "git add foo.py && git commit -m x",
        "git status && git add -- specific.py", "sudo git add -- only.py",
        'git commit -m "fixed -a flag"',            # -a only inside the message
        'echo "git add -A"',                        # quoted text, not a command
        "echo git add -A",                          # echo arg, not a git command
    ]
    for c in allow:
        assert ca._is_blanket_git(c) is False, f"should allow: {c!r}"


def test_is_blanket_git_skips_heredoc_body():
    # A blanket add inside a heredoc BODY is data, not a command.
    heredoc = "cat > script.sh <<'EOF'\ngit add -A\ngit commit -am all\nEOF"
    assert ca._is_blanket_git(heredoc) is False
    # …but a real blanket add to the right of the heredoc IS still caught.
    assert ca._is_blanket_git(heredoc + "\ngit add -A") is True


def _git_init(repo):
    import subprocess
    for args in (["init", "-q"], ["config", "user.email", "t@t"],
                 ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=repo, capture_output=True)


def test_run_command_refuses_blanket_add_without_executing(tmp_path):
    """A blanket `git add -A && git commit` is NOT executed: the user's dirty
    file is never swept, no commit lands, and a soft block dict is returned."""
    import subprocess
    repo = str(tmp_path)
    _git_init(repo)
    (tmp_path / "seed.txt").write_text("s\n")
    subprocess.run(["git", "add", "seed.txt"], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=repo, capture_output=True)
    (tmp_path / "userdirt.txt").write_text("user edit\n")   # pre-existing dirt

    res = ca._t_run_command({"cmd": "git add -A && git commit -m wip"}, repo)
    assert res["ok"] is False
    assert res["blocked"] == "blanket_git"
    assert "git add" in res["error"]
    # No new commit landed; the user's dirt is untouched and still untracked.
    log = subprocess.run(["git", "log", "--oneline"], cwd=repo,
                         capture_output=True, text=True).stdout
    assert log.count("\n") == 1                # only the seed commit
    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo,
                            capture_output=True, text=True).stdout
    assert "userdirt.txt" in status


def test_run_command_allows_targeted_add(tmp_path):
    """A targeted `git add <path>` is executed normally (not refused)."""
    import subprocess
    repo = str(tmp_path)
    _git_init(repo)
    (tmp_path / "mine.py").write_text("x = 1\n")
    res = ca._t_run_command({"cmd": "git add mine.py"}, repo)
    assert res["ok"] is True, res
    staged = subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=repo,
                            capture_output=True, text=True).stdout
    assert "mine.py" in staged


def test_run_command_blanket_subshell_no_separator_refused(tmp_path):
    """The no-separator `(git add -A)` form is caught and refused — it does
    NOT slip past as one unrecognized token."""
    import subprocess
    repo = str(tmp_path)
    _git_init(repo)
    (tmp_path / "userdirt.txt").write_text("dirt\n")
    for cmd in ("(git add -A)", "(git add -A && git commit -m wip)",
                "sudo git add -A", "git add -A && git commit && git push"):
        res = ca._t_run_command({"cmd": cmd}, repo)
        assert res.get("blocked") == "blanket_git", f"not refused: {cmd}"
        # nothing was staged
        staged = subprocess.run(["git", "diff", "--cached", "--name-only"],
                                cwd=repo, capture_output=True, text=True).stdout
        assert staged.strip() == "", f"swept by: {cmd}"


def test_run_chat_agent_blanket_becomes_observation(tmp_path):
    """End-to-end: a blanket add issued by the model surfaces as a tool result
    with blocked=blanket_git (an OBSERVATION) and never commits."""
    import subprocess
    repo = str(tmp_path)
    _git_init(repo)
    (tmp_path / "userdirt.txt").write_text("noise\n")
    fn = _scripted([
        'ACTION: run_command\nARGS_JSON: {"cmd": "git add -A && git commit -m wip"}',
        "FINAL: done",
    ])
    evs = _collect(ca.run_chat_agent(
        [{"role": "user", "content": "go"}], cwd=repo, complete_fn=fn))
    cmd_tools = [e for e in evs if e["type"] == "tool" and e["name"] == "run_command"]
    assert cmd_tools
    assert cmd_tools[0]["result"].get("blocked") == "blanket_git"
    # No commit created in the fresh (commit-less) repo.
    log = subprocess.run(["git", "log", "--oneline"], cwd=repo,
                         capture_output=True, text=True)
    assert log.returncode != 0 or log.stdout.strip() == ""
