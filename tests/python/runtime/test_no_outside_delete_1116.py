"""Approvals off never auto-confirms a delete outside the project.

Live case: a fresh OrderService run, approvals off, ran
`rm -rf ~/.m2/repository/com/acme` (the machine's shared Maven cache)
dozens of times, each auto-confirmed with reason=approvals_off.
"""
import os

from aiforge_core.runtime.tools import delete_guard as G
from aiforge_core.runtime.chat_agent import _shell


def test_outside_targets(tmp_path, monkeypatch):
    root = str(tmp_path / "repo")
    os.makedirs(root)
    home_m2 = os.path.expanduser("~/.m2/repository/com/acme")
    assert G.outside_targets(f"cd {root} && rm -rf ~/.m2/repository/com/acme", root) == \
        ["~/.m2/repository/com/acme"]
    assert G.outside_targets(f"rm -rf {home_m2}", root) == [home_m2]
    assert G.outside_targets("rm -rf target build/out", root) == []          # inside
    assert G.outside_targets("rm -rf /tmp/scratch", root) == []              # temp
    assert G.outside_targets("ssh box 'rm -rf /var/app'", root) == []        # remote
    monkeypatch.setattr(G, "_TMP_ROOTS", ())       # pytest's tmp_path is under /tmp
    assert G.outside_targets(f"cd {root}/.. && rm -rf other", root) == ["other"]
    assert G.outside_targets("mvn dependency:purge-local-repository", root)


def test_shell_refuses_a_marked_outside_delete():
    r = _shell._run_refusal("rm -rf ~/.m2/x", {"_refuse_outside_delete": ["~/.m2/x"]}, "/r")
    assert r["blocked"] == "delete_outside" and "OUTSIDE the project" in r["error"]


def test_gate_marks_outside_delete_when_approvals_off(tmp_path, monkeypatch):
    from aiforge_core.runtime.chat_agent._turn import _approval as A
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setattr(A, "_is_destructive_delete", lambda cmd, cwd: True)
    monkeypatch.setattr(A, "_commit_auto_approved", lambda *a: False)
    monkeypatch.setattr(A, "_removes_vcs_history", lambda *a: False)
    args = {"cmd": "rm -rf ~/.m2/repository/com/acme"}
    dd, _ = A._command_gate_flags("run_command", args, str(root), 1, False)
    assert args.get("_refuse_outside_delete") and not args.get("confirm_delete")
    inside = {"cmd": "rm -rf target"}
    monkeypatch.setattr(A, "_delete_pre_confirmed", lambda *a: True)
    A._command_gate_flags("run_command", inside, str(root), 1, False)
    assert inside.get("confirm_delete") and not inside.get("_refuse_outside_delete")
