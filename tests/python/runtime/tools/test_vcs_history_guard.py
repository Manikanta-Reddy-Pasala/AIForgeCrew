"""Deleting a repository's version history (.git, .hg, .svn, .jj, .bzr, _darcs)
is its own floor: never pre-confirmed, never run with approvals off or with no
human attached. A live run deleted `.git` mid-task (`rm -rf .git/ && pytest`)
because approvals were off, then re-fetched the project over its own work."""
import json

import pytest

from aiforge_core.runtime.tools import delete_guard as dg


@pytest.mark.parametrize("cmd", [
    # POSIX shells, every common spelling
    "rm -rf .git",
    "rm -rf .git/",
    "cp -r app app2 && rm -rf app/.git",     # the ORIGINAL, not the copy
    # review round: realistic forms that slipped through
    "if [ -d .git ]; then rm -rf .git; fi",
    "for d in */; do rm -rf \"$d/.git\"; done",
    "{ rm -rf .git; }",
    "bash -c 'rm -rf .git'",
    "sh -c \"cd repo && rm -rf .git\"",
    "cmd /c \"rmdir /s /q .git\"",
    "timeout 60 rm -rf .git",
    "nice -n 10 rm -rf .git",
    "sudo -u bob rm -rf .git",
    "cp ../tpl/config.yml . && rm -rf .git",          # copy INTO . is no exemption
    "rsync -a /tmp/upstream/ ./ && rm -rf .git",
    "rm -rf x/.git; cp -r . x",                       # removal BEFORE the copy
    "rm -rf .git/*",
    "rm -rf .git/objects",
    "rm -rf .git/refs/heads",
    "find . -name .git -print0 | xargs -0 rm -rf",
    "find . -name .git | xargs rm -rf",
    "Get-ChildItem -Force .git | Remove-Item -Recurse",
    "rm -rf \"$(git rev-parse --show-toplevel)/.git\"",
    "rm -rf $(git rev-parse --git-dir)",
    "mv -t /tmp .git",
    "rm -rf .??*",
    "rm -rf .g*",
    "gio trash .git",
    "git reflog expire --expire=now --all && git gc --prune=now",
    "rm -r ./.git",
    "rm -fr repo/.git",
    'rm -rf "/home/u/work/repo/.git"',
    "cd /home/u/repo && rm -rf .git/ && python3 -m pytest tests -q 2>&1 | tail",
    "rm -rf .git && git init && git add -A",
    "sudo rm -rf .git",
    "env LC_ALL=C rm -rf .git",
    "rm -rf .git*",
    "rm -rf .*",
    "rm -rf .[!.]*",
    "/bin/rm -rf .git",
    "rmdir .git",
    "mv .git /tmp/old-git",
    "mv .git .git.bak && git init",
    "trash .git",
    "rimraf .git",
    "npx rimraf .git",
    "find . -name .git -type d -exec rm -rf {} +",
    "find . -name '.git' -delete",
    # other VCS
    "rm -rf .hg",
    "rm -rf .svn",
    "rm -rf .jj",
    "rm -rf .bzr",
    "rm -rf _darcs",
    # Windows
    "rmdir /s /q .git",
    "rd /s /q .git",
    "del /s /q .git",
    "Remove-Item -Recurse -Force .git",
    "Remove-Item .git -Recurse -Force",
    "ri -r -fo .git",
    # inline scripts in many languages
    "python -c \"import shutil; shutil.rmtree('.git')\"",
    "python3 -c 'import shutil;shutil.rmtree(\"repo/.git\", ignore_errors=True)'",
    "node -e \"require('fs').rmSync('.git', {recursive: true, force: true})\"",
    "ruby -e \"require 'fileutils'; FileUtils.rm_rf('.git')\"",
    "yaegi -e 'os.RemoveAll(\".git\")'",
    "cargo script -e 'std::fs::remove_dir_all(\".git\")'",
    "pwsh -c \"[IO.Directory]::Delete('.git', $true)\"",
])
def test_history_deletion_caught(cmd):
    assert dg.removes_vcs_history(cmd) is True, cmd


@pytest.mark.parametrize("cmd", [
    "rm -f .git/index.lock",            # stale lock: routine, inside the folder
    "rm .git/MERGE_HEAD",
    "rm -rf .github/workflows/old.yml",
    "rm .gitignore",
    "rm -f .gitmodules .gitattributes",
    "rm -rf .svnignore",
    "git rm --cached file.py",
    "git init",
    "git status && ls .git",
    "cat .git/HEAD",
    "cd .git && ls; rm -f build.log",   # the rm is about another file
    "cp -r .git /tmp/backup-git",      # copy keeps the history
    "mv src/a.py src/b.py",
    "mv old.git.txt new.txt",
    "rm -rf build dist .pytest_cache node_modules",
    "rm -rf ./*",                       # does not expand to dotfiles
    "find . -name '*.pyc' -delete",
    "find . -name .git -type d",        # only lists
    "python -c \"import shutil; shutil.rmtree('build')\"",
    "python -c \"open('.gitignore').read()\"",
    "grep -r 'rm -rf .git' docs/",      # the words, not the act
    "echo 'never rm -rf .git' > NOTE.txt",
    "ls -la",
    # review round: ordinary work that must keep running
    "python3 - <<'EOF'\nimport os\nfor root, dirs, files in os.walk('.'):\n    if '.git' in dirs: dirs.remove('.git')\nEOF",
    "python -c \"import os; [d for d in os.walk('.') if 1]; x=['.git']; x.remove('.git')\"",
    "grep -rn \"rmtree('.git')\" .",
    "find . -name .git -prune -o -name '*.pyc' -delete",
    "find . -not -path '*/.git/*' -name '*.orig' -delete",
    "find . -name .gitkeep -delete",
    "find . -name .gitignore -delete",
    "git clone --depth 1 https://x/y.git vendor/lib && rm -rf vendor/lib/.git",
    "git clone https://x/proj.git && rm -rf proj/.git",
    "cp -r repo /tmp/ && rm -rf /tmp/repo/.git",
    "cat > Dockerfile <<'EOF'\nRUN git clone https://x/y /opt/x && rm -rf /opt/x/.git\nEOF",
    "echo \"(rm -rf .git) is bad\" >> NOTES.md",
    "echo \"a && rm -rf .git\"",
    "rm -f .*.swp",
    "docker exec web rm -rf /app/.git",     # another machine's files
    "git reflog expire --expire=90.days.ago --all",
    "git gc",
    # history of a scratch COPY made in the same command: nothing is lost
    "cd repos && cp -r sqlglot sqlglot_orig && rm -rf sqlglot_orig/.git",
    "rsync -a app/ /tmp/app-snap && rm -rf /tmp/app-snap/.git",
    "",
])
def test_ordinary_commands_not_caught(cmd):
    assert dg.removes_vcs_history(cmd) is False, cmd


def test_refusal_names_the_folder():
    assert ".hg" in dg.vcs_history_refusal("rm -rf .hg")
    assert ".git" in dg.vcs_history_refusal("rm -rf .git/")


# ── the floor holds where ordinary deletes are let through ──────────────────
def _run_gate(monkeypatch, cmd, *, session_id, approvals):
    from aiforge_core.runtime import chat_approve
    from aiforge_core.runtime.chat_agent._turn import _approval
    monkeypatch.setattr(chat_approve, "approvals_required",
                        lambda sid: True if sid is None else approvals)
    convo = []
    gen = _approval._approval_gate("run_command", {"cmd": cmd}, "/tmp",
                                   session_id, convo)
    events = []
    try:
        while True:
            events.append(next(gen))
    except StopIteration as stop:
        return events, stop.value, convo


def test_refused_with_approvals_off(monkeypatch):
    events, ret, convo = _run_gate(monkeypatch, "rm -rf .git/ && pytest -q",
                                   session_id=7, approvals=False)
    assert ret == "continue"
    assert events[0]["result"]["blocked"] == "vcs_history"
    assert "version history" in json.loads(
        convo[-1]["content"].split("OBSERVATION: ", 1)[1])["error"]


def test_refused_with_no_human(monkeypatch):
    events, ret, _ = _run_gate(monkeypatch, "rm -rf .git",
                               session_id=None, approvals=True)
    assert ret == "continue"
    assert events[0]["result"]["blocked"] == "vcs_history"


def test_refused_in_sandbox_with_approvals_off(monkeypatch):
    monkeypatch.setenv("AIFORGE_SANDBOX", "1")
    monkeypatch.setenv("AIFORGE_CHAT_ALLOW_DELETE", "1")
    events, ret, _ = _run_gate(monkeypatch, "rm -rf .git",
                               session_id=3, approvals=False)
    assert events[0]["result"]["blocked"] == "vcs_history"


def test_asks_the_human_when_approvals_on(monkeypatch):
    from aiforge_core.runtime.chat_agent._turn import _approval
    flags = _approval._command_gate_flags(
        "run_command", {"cmd": "rm -rf .git"}, "/tmp", 5, True)
    assert flags == (True, False)          # gated as a delete, never auto


def test_ordinary_delete_unchanged_with_approvals_off(monkeypatch):
    """`rm -rf build` with approvals off still runs (no new prompt)."""
    from aiforge_core.runtime.chat_agent._turn import _approval
    args = {"cmd": "rm -rf build"}
    destructive, _ = _approval._command_gate_flags(
        "run_command", args, "/tmp", 5, False)
    assert destructive is False and args.get("confirm_delete") is True


def test_doer_bash_refuses(monkeypatch, tmp_path):
    from aiforge_core.runtime.tools import bash
    monkeypatch.setenv("AIFORGE_ALLOW_DELETE", "1")
    (tmp_path / ".git").mkdir()
    out = bash.bash(f"cd {tmp_path} && rm -rf .git")
    assert out["ok"] is False and out.get("blocked") == "vcs_history"
    assert (tmp_path / ".git").exists()


def test_whole_repo_delete_needs_cwd(tmp_path, monkeypatch):
    monkeypatch.setattr(dg, "_TMP_ROOTS", ())          # pytest's tmp is /tmp
    proj = tmp_path / "work" / "proj"
    (proj / ".git").mkdir(parents=True)
    (tmp_path / "work" / "build").mkdir()
    base = str(tmp_path / "work" / "proj")
    assert dg.removes_vcs_history("cd .. && rm -rf proj && git clone U proj", base)
    assert dg.removes_vcs_history("rm -rf ../proj", base)
    assert not dg.removes_vcs_history("rm -rf ../build", base)
    assert not dg.removes_vcs_history("rm -rf ../proj", None)   # no cwd: unknown


def test_scratch_repo_in_tmp_is_free():
    assert not dg.removes_vcs_history("rm -rf /tmp/scratch-repo", "/home/u")


def test_code_cell_detection():
    assert dg.code_removes_vcs_history("import shutil\nshutil.rmtree('.git')")
    assert not dg.code_removes_vcs_history("dirs.remove('.git')")


def test_command_risk_marks_history_dangerous_even_in_sandbox(monkeypatch):
    from aiforge_core.runtime.tools import command_risk
    monkeypatch.setenv("AIFORGE_SANDBOX", "1")
    v = command_risk.assess("rm -rf .git")
    assert v["level"] == command_risk.DANGEROUS and "history" in v["reason"]
    assert command_risk.assess("rm -rf build")["level"] != command_risk.DANGEROUS


def test_cell_gate_refuses(monkeypatch):
    from aiforge_core.runtime import chat_approve
    from aiforge_core.runtime.chat_agent._turn import _approval
    monkeypatch.setattr(chat_approve, "approvals_required", lambda sid: False)
    gen = _approval._approval_gate(
        "execute_ipython_cell", {"code": "!rm -rf .git"}, "/tmp", 4, [])
    ev = next(gen)
    assert ev["result"]["blocked"] == "vcs_history"


def test_model_cannot_forge_the_approval(monkeypatch):
    from aiforge_core.runtime import chat_approve
    from aiforge_core.runtime.chat_agent._turn import _approval
    monkeypatch.setattr(chat_approve, "approvals_required", lambda sid: False)
    args = {"cmd": "rm -rf .git", "confirm_delete": True,
            dg.VCS_APPROVED_KEY: True}
    ev = next(_approval._approval_gate("run_command", args, "/tmp", 4, []))
    assert ev["result"]["blocked"] == "vcs_history"
    assert dg.VCS_APPROVED_KEY not in args


def test_shell_floor_refuses_model_confirmed(tmp_path):
    from aiforge_core.runtime.chat_agent import _shell
    (tmp_path / ".git").mkdir()
    out = _shell._run_refusal("rm -rf .git", {"confirm_delete": True},
                              str(tmp_path))
    assert out["blocked"] == "vcs_history"
    assert _shell._run_refusal("rm -rf .git", {dg.VCS_APPROVED_KEY: True,
                                               "confirm_delete": True},
                               str(tmp_path)) is None


def test_serve_and_kernel_helper_refuse():
    from aiforge_core.runtime.tools import agentskills, serve
    assert serve._delete_refusal("rm -rf .git && npm run dev",
                                 {"confirm_delete": True})["blocked"] == "vcs_history"
    ns = {}
    exec(agentskills.BOOTSTRAP_SOURCE, ns)             # the kernel helper
    assert ns["run_cmd"]("rm -rf .git")["blocked"] == "vcs_history"
