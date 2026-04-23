"""Static config for the orchestrator.

Everything lives in code — no YAML, no env explosion. Override per-deploy via
the `AIFORGE_RUNTIME_*` env vars at the bottom.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


# ─────────────────────────────── DSNs ───────────────────────────────
AIFORGE_DSN = os.environ.get(
    "AIFORGE_DSN",
    "postgresql://manikanta@127.0.0.1:5432/aiforge",
)

# ─────────────────────────── Inference endpoints ────────────────────────
LM_STUDIO_BASE_URL = os.environ.get("AIFORGE_LM_BASE_URL", "http://127.0.0.1:1234/v1")
LM_STUDIO_API_KEY  = os.environ.get("AIFORGE_LM_API_KEY", "lm-studio")

CLAUDE_BIN = os.environ.get("AIFORGE_CLAUDE_BIN", "claude")
CLAUDE_MODEL = os.environ.get("AIFORGE_CLAUDE_MODEL", "claude-opus-4-7")

EMBED_SIDECAR_URL   = os.environ.get("AIFORGE_EMBED_URL", "http://127.0.0.1:8764")
RERANK_SIDECAR_URL  = os.environ.get("AIFORGE_RERANK_URL", "http://127.0.0.1:8765")

# ─────────────────────────── Paths ──────────────────────────────────────
LOG_DIR  = os.environ.get("AIFORGE_LOG_DIR",  os.path.expanduser("~/.aiforge/logs"))
LOCK_DIR = os.environ.get("AIFORGE_LOCK_DIR", "/tmp")
WORKTREE_ROOT = os.environ.get(
    "AIFORGE_WORKTREE_ROOT",
    os.path.expanduser("~/codeRepo"),
)

# ─────────────────────────── Tick budget ────────────────────────────────
TICK_MAX_WALL_SECS = int(os.environ.get("AIFORGE_TICK_MAX_WALL", "2400"))   # 40 min
# Hard ceiling for ticket.metadata.max_turns overrides. Actual budget is
# min(wall_secs, max_turns); wall is the true guardrail. Raised 120→300
# so comprehensive analysis / huge README tickets can set 150+ without
# being silently clamped.
TICK_MAX_TURNS     = int(os.environ.get("AIFORGE_TICK_MAX_TURNS", "500"))


@dataclass(frozen=True)
class RoleConfig:
    name: str                # supervisor | planner | doer | feedback | learner
    model: str               # LM Studio model id OR "claude-cli"
    transport: str           # "openai" | "claude_cli"
    max_turns: int
    tool_allowlist: tuple[str, ...]
    identity_prefix: str = "" # appended to system prompt
    ctx: int = 32768         # LM Studio context length memguard requests
    ttl_s: int = 1800        # LM Studio TTL; 8h (28800) for hot roles

    @property
    def lock_path(self) -> str:
        # Per-instance lock so multiple tick workers for the same role can
        # run in parallel. Set AIFORGE_TICK_INSTANCE=a|b|c... on each
        # launchd plist to spawn N concurrent workers. claim_next uses
        # FOR UPDATE SKIP LOCKED so row-level claims are race-safe.
        instance = os.environ.get("AIFORGE_TICK_INSTANCE", "a")
        return f"{LOCK_DIR}/aiforge-tick-{self.name}-{instance}.lock"


# Tool allowlists per role. Narrower = less distraction + less mischief.
_DOER_TOOLS = (
    "search", "read_file", "grep_repo", "run_shell", "fetch_url",
    "write_file", "edit", "git_commit", "git_push",
    # create_child_ticket intentionally REMOVED — Doer must not spawn
    # retry-children. On compile-red / spec gap it escalates back to
    # Planner via update_assignee (label=doer-blocked), never recurses.
    "post_comment", "set_status", "retain_fact",
    "update_assignee",   # escalate back to planner on compile-red / spec gap
    "related_tickets", "graph_neighbors", "kubectl_read", "read_claude_memory",
)
_PLANNER_TOOLS = (
    "search", "read_file", "grep_repo", "run_shell", "fetch_url",
    "create_child_ticket", "post_comment", "set_status", "retain_fact",
    "update_assignee",   # escalation to supervisor when stuck
    "related_tickets", "graph_neighbors", "kubectl_read", "mongo_query",
    "read_claude_memory",
)
_SUPERVISOR_TOOLS = (
    # Supervisor now has triage + rescue + audit duty. Needs read + grep +
    # child creation + routing. No write/edit/commit — always delegates.
    "search", "read_file", "grep_repo", "post_comment",
    "create_child_ticket", "update_assignee",
    "related_tickets", "graph_neighbors", "read_claude_memory",
)
_FEEDBACK_TOOLS = (
    # No set_status — verdict_pass / verdict_fail handle status transitions.
    "search", "read_file", "run_shell",
    "post_comment", "verdict_pass", "verdict_fail",
    "related_tickets", "read_claude_memory",
)
_LEARNER_TOOLS = (
    "search", "retain_fact", "post_comment", "set_status",
    "related_tickets", "read_claude_memory",
)


# ─────────────────────────── Models ─────────────────────────────────────
# Cross-family diversity: Google MoE (Supervisor), Qwen MoE (Planner),
# Qwen dense (Doer), Mistral dense (Feedback), Qwen small (Learner).
# All models already on disk; change here + git push to swap.
# Doer's model is the ONLY one protected (always hot). Other roles'
# models are free to differ — memguard evicts non-protected to fit when
# a role's tick needs a different LLM. Currently only Planner + Doer
# ticks run; Supervisor / Feedback / Learner kept in config for re-enable
# but their plists aren't installed.
SUPERVISOR_MODEL = os.environ.get("AIFORGE_SUPERVISOR_MODEL", "qwen3.6-35b-a3b")
PLANNER_MODEL    = os.environ.get("AIFORGE_PLANNER_MODEL",    "qwen3.6-35b-a3b")
DOER_MODEL       = os.environ.get("AIFORGE_DOER_MODEL",       "qwen3-coder-next")
FEEDBACK_MODEL   = os.environ.get("AIFORGE_FEEDBACK_MODEL",   "qwen3.6-35b-a3b")
LEARNER_MODEL    = os.environ.get("AIFORGE_LEARNER_MODEL",    "qwen3.6-35b-a3b")

# Supervisor transport — flip to claude_cli for cloud oversight on tough
# routing calls. Default = local gemma-4-26b (fast + fits memory budget).
SUPERVISOR_TRANSPORT = os.environ.get("AIFORGE_SUPERVISOR_TRANSPORT", "openai")


ROLES: dict[str, RoleConfig] = {
    "supervisor": RoleConfig(
        name="supervisor",
        model=(CLAUDE_MODEL if SUPERVISOR_TRANSPORT == "claude_cli"
               else SUPERVISOR_MODEL),
        transport=SUPERVISOR_TRANSPORT,
        max_turns=15,   # was 4 — triage+rescue+audit needs headroom
        tool_allowlist=_SUPERVISOR_TOOLS,
        ctx=16384, ttl_s=1800,
    ),
    "planner": RoleConfig(
        name="planner",
        model=PLANNER_MODEL,
        transport="openai",
        max_turns=25,
        tool_allowlist=_PLANNER_TOOLS,
        ctx=32768, ttl_s=28800,   # hot; 8h TTL; gpt-oss-20b non-vision parallel=4
    ),
    "doer": RoleConfig(
        name="doer",
        model=DOER_MODEL,
        transport="openai",
        max_turns=60,
        tool_allowlist=_DOER_TOOLS,
        ctx=131072, ttl_s=28800,  # hot; 8h TTL
    ),
    "feedback": RoleConfig(
        name="feedback",
        model=FEEDBACK_MODEL,
        transport="openai",
        max_turns=6,
        tool_allowlist=_FEEDBACK_TOOLS,
        ctx=16384, ttl_s=1800,
    ),
    "learner": RoleConfig(
        name="learner",
        model=LEARNER_MODEL,
        transport="openai",
        max_turns=4,
        tool_allowlist=_LEARNER_TOOLS,
        ctx=16384, ttl_s=1800,
    ),
}


# Legacy role-name aliases — older ticket rows still carry these as
# `assignee_role`. Map them to current roles so ticks don't break.
_LEGACY_ALIASES = {
    "architect": "supervisor",
    "sr_developer": "planner",
    "developer": "doer",
    "fact_extract": "learner",
}


def role(name: str) -> RoleConfig:
    name = _LEGACY_ALIASES.get(name, name)
    try:
        return ROLES[name]
    except KeyError as exc:
        raise KeyError(f"unknown role {name!r}; valid: {list(ROLES)}") from exc


def canonical_role(name: str) -> str:
    """Return the canonical role name for a legacy or current role name."""
    return _LEGACY_ALIASES.get(name, name)
