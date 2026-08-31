"""Approvals OFF in an interactive chat run is itself the delete confirmation.

The destructive-delete floor used to ignore the per-mode approval toggle, which
made the toggle feel broken: the guard matches the whole command string, so
routine remote maintenance (`ssh host 'docker rm -f c'`, `kubectl delete pod`,
`git clean -fdx`) kept prompting after approvals had been turned off.

The relaxation is narrow ON PURPOSE, and these tests pin the boundary — an
autonomous run and an approvals-ON mode must still be gated.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def chat(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("AIFORGE_CHAT_ALLOW_DELETE", raising=False)
    monkeypatch.delenv("AIFORGE_ALLOW_DELETE", raising=False)
    from aiforge_core.config import approval_settings
    from aiforge_core.runtime import chat_approve
    from aiforge_core.runtime.chat_agent import _loop
    from aiforge_core.runtime.tools import tool_policy
    return approval_settings, chat_approve, _loop, tool_policy


def _gate(_loop, tool_policy, cmd: str, sid):
    args = {"cmd": cmd}
    verdict = tool_policy.decide("run_command", args)
    gate, ddel, _fr, _b = _loop._compute_gate_decision(
        "run_command", args, "/tmp", sid, verdict)
    return gate, ddel, args


DELETES = [
    "ssh nuc 'docker rm -f aiforge-sonar-scan'",
    "ssh nuc 'kubectl delete pod foo -n pos'",
    "ssh nuc 'git clean -fdx'",
    "rm -rf build",
]


@pytest.mark.parametrize("cmd", DELETES)
def test_approvals_off_lets_an_interactive_delete_run(chat, cmd):
    approval_settings, chat_approve, _loop, tool_policy = chat
    sid = 4242
    chat_approve.set_mode(sid, "simple")
    approval_settings.set_mode("chat", False)

    gate, ddel, args = _gate(_loop, tool_policy, cmd, sid)
    assert not gate, f"{cmd!r} still prompts with approvals off"
    assert not ddel
    assert args.get("confirm_delete") is True, \
        "the decision must carry through to the shell tool's own refusal"


@pytest.mark.parametrize("cmd", DELETES)
def test_approvals_on_still_gates_the_same_delete(chat, cmd):
    approval_settings, chat_approve, _loop, tool_policy = chat
    sid = 4242
    chat_approve.set_mode(sid, "simple")
    approval_settings.set_mode("chat", True)

    gate, ddel, args = _gate(_loop, tool_policy, cmd, sid)
    assert gate, f"{cmd!r} must still gate when approvals are ON"
    assert ddel, f"{cmd!r} must still read as a destructive delete"
    assert "confirm_delete" not in args


@pytest.mark.parametrize("cmd", DELETES)
def test_autonomous_run_is_never_relaxed(chat, cmd):
    """THE boundary. session_id None = an unattended ticket run: no human is
    watching, so approvals_required(None) is True by design and this relaxation
    must be unreachable — whatever the saved toggle says."""
    approval_settings, chat_approve, _loop, tool_policy = chat
    for mode in ("simple", "plan", "team"):
        approval_settings.set_mode(mode, False)

    gate, ddel, args = _gate(_loop, tool_policy, cmd, None)
    assert ddel, f"{cmd!r} must stay a destructive delete for an autonomous run"
    assert gate
    assert "confirm_delete" not in args


def test_turning_off_chat_does_not_relax_plan_or_pipeline(chat):
    """Each mode carries its own toggle; the relaxation must not leak sideways."""
    approval_settings, chat_approve, _loop, tool_policy = chat
    sid = 99
    approval_settings.set_mode("chat", False)
    approval_settings.set_mode("plan", True)
    approval_settings.set_mode("pipeline", True)

    for mode in ("plan", "team"):
        chat_approve.set_mode(sid, mode)
        gate, ddel, _ = _gate(_loop, tool_policy, "rm -rf build", sid)
        assert gate, f"{mode} approvals are ON — it must still gate"
        assert ddel, f"{mode} must still read as a destructive delete"

    chat_approve.set_mode(sid, "simple")
    gate, _, _ = _gate(_loop, tool_policy, "rm -rf build", sid)
    assert not gate, "chat approvals are OFF — it runs"


def test_non_delete_commands_are_unaffected(chat):
    approval_settings, chat_approve, _loop, tool_policy = chat
    sid = 4242
    chat_approve.set_mode(sid, "simple")
    approval_settings.set_mode("chat", True)          # approvals ON
    for cmd in ("ls -la", "ssh nuc 'docker ps'", "npm run build"):
        gate, _, _ = _gate(_loop, tool_policy, cmd, sid)
        assert not gate, f"{cmd!r} was never gated and must not start"
