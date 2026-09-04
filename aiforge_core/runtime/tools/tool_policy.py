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

# Tools whose args carry a shell command to risk-assess. ``serve`` launches an
# arbitrary command as a background process, so it must be risk-assessed too —
# else `serve {"cmd": "curl … | sh"}` runs unassessed while the same command via
# run_command/bash escalates to ASK.
# ``watch_until`` carries a shell command under the same ``cmd`` key and runs
# it up to max_checks times, so it must be risk-assessed exactly like the
# others — otherwise it is a hole straight through the approval gate, the
# operator's run_command=deny policy, and any PreToolUse hook matching
# run_command: `watch_until {"cmd": "git push --force …"}` would have executed
# twenty times, unattended.
# ``ui_check`` starts the app it is about to look at, by handing its ``cmd``
# to ``serve`` — the same shell. Leaving it out made it a hole straight past
# the gate that serve itself sits behind: `ui_check {"cmd": "curl … | sh"}`
# ran unassessed while the identical string via serve escalated to ASK.
_CMD_TOOLS = {"run_command", "bash", "shell", "run_shell", "serve",
              "watch_until", "ui_check"}
_CMD_ARG_KEYS = ("cmd", "command", "input")

# Tools whose args carry CODE that can reach a shell from the inside. A cell
# is a shell with three extra characters (`!curl x | sh`, os.system,
# subprocess), so leaving it out of the risk path meant the identical string
# escalated to ASK via bash and ran unassessed via the kernel.
_CODE_TOOLS = {"execute_ipython_cell"}
_CODE_ARG_KEYS = ("code", "cell", "source")

# Tools that mutate something external/durable → default to ASK (human
# approval) unless the operator explicitly overrides via AIFORGE_TOOL_POLICY.
# Read-only filesystem + search tools — inspecting the tree is never worth an
# approval prompt. Pinned to ALLOW so nothing (forced-review, risk escalation,
# a future default) can gate them; an explicit per-tool AIFORGE_TOOL_POLICY
# entry still wins. Command tools (bash/shell) are NOT here — they stay risk-
# assessed, so `grep`-ing a secret path still gates.
_READONLY_ALWAYS_ALLOW = {
    "grep_repo", "grep", "list_dir", "ls", "glob", "repo_map", "search",
    "find", "file_read", "read", "memory_lookup", "skill_search",
    "workflow_search", "graphify_lookup",
    # git inspect + jira/confluence reads are read-only too — never prompt.
    "git_status", "git_diff", "git_log", "git_blame", "jira_transitions",
    "jira_worklog", "jira_myself", "jira_projects", "jira_boards",
    "jira_remote_links", "jira_comments", "context_gather",
    "resolve_repo",
    "jira_resolve_project", "confluence_resolve_space",
    "jira_sprints", "jira_sprint_issues", "jira_dashboards",
    "jira_dashboard_read", "confluence_children", "confluence_spaces",
    "confluence_page_by_title", "confluence_labels", "confluence_comments",
    "confluence_descendants", "read_lines",
    # GitLab reads, including CI. `gitlab_search`/`gitlab_read` were missing
    # from this list too — the sibling comment in _registry._READONLY_TOOLS
    # says the two classifications must stay in sync, and 24 entries had
    # drifted. Unknown names default to ALLOW today, so this pins the intent
    # rather than changing behaviour: without it, any future tightening of the
    # default would start gating read-only tools.
    "gitlab_search", "gitlab_read",
    "gitlab_pipelines", "gitlab_pipeline", "gitlab_pipeline_watch",
}

_DEFAULT_ASK = {"confluence_create", "confluence_update",
                "confluence_add_label", "confluence_comment",
                "jira_create", "jira_update", "jira_comment", "jira_log_work",
                "jira_dashboard_create",
                "jira_transition", "jira_assign", "jira_link_issues",
                "confluence_attach",
                "gitlab_create", "gitlab_update", "gitlab_comment",
                "gitlab_mr_create", "gitlab_mr_comment", "github_pr",
                # Sends an email out to real recipients — approval-gated.
                "email_send",
                # Arbitrary-code execution in a live kernel — approval-gated
                # in chat like Claude Code / Cursor gate code execution.
                "execute_ipython_cell",
                # Installs a RECURRING host shell job (writes a script + cron) —
                # a durable, self-executing action; confirm before it lands.
                "create_job_script",
                # Same shape: schedules recurring autonomous work that outlives
                # the chat (each run files a ticket the pipeline builds), and
                # `cancel` deletes a job row for good — including one the
                # operator created in the Jobs UI.
                "schedule_task"}


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


def _arg_str(args: dict | None, keys) -> str:
    if not isinstance(args, dict):
        return ""
    for k in keys:
        v = args.get(k)
        if isinstance(v, str) and v:
            return v
    return ""


def _cmd_from_args(args: dict | None) -> str:
    return _arg_str(args, _CMD_ARG_KEYS)


def _code_from_args(args: dict | None) -> str:
    return _arg_str(args, _CODE_ARG_KEYS)


def _egress_refusal(tool: str, args: dict | None) -> str:
    """Why this call may not reach the network, or "".

    Covers both transports the agent reaches for after a refusal: a shell
    command (curl / wget / nc) and a notebook cell that shells out. The cell's
    LIBRARY calls — requests, urllib, a raw socket — cannot be caught here at
    all; ``runtime.tools.kernel_egress`` guards those inside the kernel.
    """
    if tool not in _CMD_TOOLS and tool not in _CODE_TOOLS:
        return ""
    try:
        from aiforge_core.net import egress as _eg
        lines = ([_cmd_from_args(args)] if tool in _CMD_TOOLS else
                 command_risk.shell_strings_in_code(_code_from_args(args)))
        for line in lines:
            refusal = _eg.command_refusal(line)
            if refusal:
                return f"{refusal.get('error')} — {refusal.get('hint')}"
    except Exception:  # noqa: BLE001 — a gate bug must not block every command
        return ""
    return ""


def _rank(p: str) -> int:
    return {ALLOW: 0, ASK: 1, DENY: 2}.get(p, 0)


def _risk_verdict(tool: str, args: dict | None) -> dict:
    """The classifier's verdict for a tool that carries a command or a cell —
    a cell's shell calls are lifted back out and run through the same rules
    (see ``command_risk.assess_code``). ``{}`` for every other tool."""
    if tool in _CODE_TOOLS:
        return command_risk.assess_code(_code_from_args(args))
    if tool in _CMD_TOOLS:
        return command_risk.assess(_cmd_from_args(args))
    return {}


def _ask_caution() -> bool:
    """Caution-tier (sudo, chmod 777, force-push, global installs…) gates for
    approval BY DEFAULT; AIFORGE_RISK_ASK_CAUTION=0 runs it free."""
    return os.environ.get("AIFORGE_RISK_ASK_CAUTION", "1").strip().lower() \
        not in ("0", "false", "no", "off")


def _apply_risk(tool: str, args: dict | None, policy: str,
                reason: str) -> tuple[str, str, str]:
    """Escalate ``policy`` for a risky command/cell. Returns
    ``(policy, reason, risk_level)`` — the level is the classifier's own
    answer, kept separate from the resulting policy because a gate with no
    human attached needs to know WHY it is being asked."""
    verdict = _risk_verdict(tool, args)
    if not verdict:
        return policy, reason, ""
    lvl = verdict["level"]
    escalate = (lvl == command_risk.DANGEROUS
                or (lvl == command_risk.CAUTION and _ask_caution()))
    if escalate and _rank(policy) < _rank(ASK):
        return ASK, verdict["reason"], lvl
    if lvl == command_risk.DANGEROUS and verdict["reason"]:
        # Already at ask/deny for another reason (execute_ipython_cell is ask
        # by default). Say the DANGEROUS thing anyway — an approval card
        # reading "writes to an external system" for a cell that pipes curl
        # into a shell tells the human the wrong thing to weigh.
        return policy, verdict["reason"], lvl
    return policy, reason, lvl


def decide(tool: str, args: dict | None = None) -> dict:
    """Return ``{"policy", "reason"}`` for a tool call.

    ``policy`` ∈ {allow, ask, deny}. ``reason`` explains an ask/deny
    (e.g. the risk verdict) so the UI can show *why* approval is needed.
    ``risk`` is the raw command/cell verdict ("" when this tool carries
    neither), which the gates use to refuse a DANGEROUS call outright when
    there is no human who could approve it.
    """
    cfg = _configured()
    # Read-only fs/search tools never gate (unless the operator explicitly set a
    # policy for that exact tool) — so folder listing / grep / read / recall
    # actions run without an approval prompt.
    if tool in _READONLY_ALWAYS_ALLOW and tool not in cfg:
        return {"policy": ALLOW, "reason": "", "risk": ""}
    # Mutating-external tools default to ASK; an explicit env policy still wins.
    default = ASK if tool in _DEFAULT_ASK else ALLOW
    configured = cfg.get(tool, default)
    policy, reason = configured, ""
    if tool in _DEFAULT_ASK and tool not in cfg:
        reason = f"'{tool}' writes to an external system — confirm first"

    # EGRESS first, and as a DENY. A refused web_fetch that the agent reruns as
    # `curl` — or inside a notebook cell — is the same request wearing another
    # transport, and it was going straight through: the risk classifier asks
    # whether a command is dangerous, and fetching a page is not. This is not
    # an approval question either, because what is missing is an operator's
    # allowlist entry, not a human's blessing of this one call.
    egress_refusal = _egress_refusal(tool, args)
    if egress_refusal:
        return {"policy": DENY, "reason": egress_refusal, "risk": ""}

    policy, reason, risk_level = _apply_risk(tool, args, policy, reason)

    if policy != ALLOW and not reason:
        reason = f"tool '{tool}' is set to {policy} by policy"
    # ``risk`` is the classifier's own verdict, not the resulting policy: a gate
    # with no human attached needs to know WHY it is being asked, because
    # "dangerous" is the one answer it must not degrade into an allow.
    return {"policy": policy, "reason": reason, "risk": risk_level}


__all__ = ["decide", "ALLOW", "ASK", "DENY"]
