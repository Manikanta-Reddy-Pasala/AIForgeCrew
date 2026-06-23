"""Per-tool permission policy — allow / ask / deny.

Frontier agents (opencode's permission matrix, Cline's auto-approve list)
let the operator decide, per tool, whether the agent may run it freely
(``allow``), must pause for human approval (``ask``), or is forbidden
(``deny``). AIForgeCrew historically had only a binary allowlist plus the
delete guard; this generalises it.

Resolution (most-specific wins):
  1. Per-tool env  ``AIFORGE_TOOL_POLICY="run_command=ask,file_write=deny"``
     (chat-specific override: ``AIFORGE_CHAT_TOOL_POLICY``).
  2. Risk escalation: a command-running tool whose ``command_risk`` verdict
     is ``dangerous`` is forced to at least ``ask`` (``deny`` is kept).
     With ``AIFORGE_RISK_ASK_CAUTION=1`` a ``caution`` verdict also → ask.
  3. Default ``allow``.

The decision is advisory data — the chat loop turns ``ask`` into an
approval gate (see ``chat_approve``) and ``deny`` into a hard refusal.
"""
from __future__ import annotations

import os

from . import command_risk

ALLOW = "allow"
ASK = "ask"
DENY = "deny"

_VALID = {ALLOW, ASK, DENY}

# Tools whose args carry a shell command to risk-assess.
_CMD_TOOLS = {"run_command", "bash", "shell", "run_shell"}
_CMD_ARG_KEYS = ("cmd", "command", "input")


def _parse_map(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, val = part.partition("=")
        val = val.strip().lower()
        if val in _VALID:
            out[name.strip()] = val
    return out


def _configured() -> dict[str, str]:
    m = _parse_map(os.environ.get("AIFORGE_TOOL_POLICY", ""))
    m.update(_parse_map(os.environ.get("AIFORGE_CHAT_TOOL_POLICY", "")))
    return m


def _cmd_from_args(args: dict | None) -> str:
    if not isinstance(args, dict):
        return ""
    for k in _CMD_ARG_KEYS:
        v = args.get(k)
        if isinstance(v, str) and v:
            return v
    return ""


def _rank(p: str) -> int:
    return {ALLOW: 0, ASK: 1, DENY: 2}.get(p, 0)


def decide(tool: str, args: dict | None = None) -> dict:
    """Return ``{"policy", "reason"}`` for a tool call.

    ``policy`` ∈ {allow, ask, deny}. ``reason`` explains an ask/deny
    (e.g. the risk verdict) so the UI can show *why* approval is needed.
    """
    configured = _configured().get(tool, ALLOW)
    policy, reason = configured, ""

    # Risk escalation for command-running tools.
    if tool in _CMD_TOOLS:
        verdict = command_risk.assess(_cmd_from_args(args))
        lvl = verdict["level"]
        ask_caution = os.environ.get(
            "AIFORGE_RISK_ASK_CAUTION", "").strip().lower() in ("1", "true", "yes", "on")
        if lvl == command_risk.DANGEROUS and _rank(policy) < _rank(ASK):
            policy, reason = ASK, verdict["reason"]
        elif lvl == command_risk.CAUTION and ask_caution and _rank(policy) < _rank(ASK):
            policy, reason = ASK, verdict["reason"]

    if policy != ALLOW and not reason:
        reason = f"tool '{tool}' is set to {policy} by policy"
    return {"policy": policy, "reason": reason}


__all__ = ["decide", "ALLOW", "ASK", "DENY"]
