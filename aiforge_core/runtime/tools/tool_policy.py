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
_CMD_TOOLS = {"run_command", "bash", "shell", "run_shell", "serve",
              "watch_until"}
_CMD_ARG_KEYS = ("cmd", "command", "input")

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
    cfg = _configured()
    # Read-only fs/search tools never gate (unless the operator explicitly set a
    # policy for that exact tool) — so folder listing / grep / read / recall
    # actions run without an approval prompt.
    if tool in _READONLY_ALWAYS_ALLOW and tool not in cfg:
        return {"policy": ALLOW, "reason": ""}
    # Mutating-external tools default to ASK; an explicit env policy still wins.
    default = ASK if tool in _DEFAULT_ASK else ALLOW
    configured = cfg.get(tool, default)
    policy, reason = configured, ""
    if tool in _DEFAULT_ASK and tool not in cfg:
        reason = f"'{tool}' writes to an external system — confirm first"

    # Risk escalation for command-running tools.
    if tool in _CMD_TOOLS:
        verdict = command_risk.assess(_cmd_from_args(args))
        lvl = verdict["level"]
        # Caution-tier (sudo, chmod 777, force-push, global installs…) gates for
        # approval BY DEFAULT now; set AIFORGE_RISK_ASK_CAUTION=0 to run it free.
        ask_caution = os.environ.get(
            "AIFORGE_RISK_ASK_CAUTION", "1").strip().lower() not in ("0", "false", "no", "off")
        if lvl == command_risk.DANGEROUS and _rank(policy) < _rank(ASK):
            policy, reason = ASK, verdict["reason"]
        elif lvl == command_risk.CAUTION and ask_caution and _rank(policy) < _rank(ASK):
            policy, reason = ASK, verdict["reason"]

    if policy != ALLOW and not reason:
        reason = f"tool '{tool}' is set to {policy} by policy"
    return {"policy": policy, "reason": reason}


__all__ = ["decide", "ALLOW", "ASK", "DENY"]
