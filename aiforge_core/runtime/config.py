"""Static config for the v5 orchestrator.

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
TICK_MAX_WALL_SECS = int(os.environ.get("AIFORGE_TICK_MAX_WALL", "1200"))   # 20 min
TICK_MAX_TURNS     = int(os.environ.get("AIFORGE_TICK_MAX_TURNS", "40"))


@dataclass(frozen=True)
class RoleConfig:
    name: str                # architect | sr_developer | developer | fact_extract
    model: str               # LM Studio model id OR "claude-cli"
    transport: str           # "openai" | "claude_cli"
    max_turns: int
    tool_allowlist: tuple[str, ...]
    identity_prefix: str = "" # appended to system prompt

    @property
    def lock_path(self) -> str:
        return f"{LOCK_DIR}/aiforge-tick-{self.name}.lock"


# Tool names are the @function_tool decorator names exported from tools.py.
# The Developer gets every tool; other roles are progressively narrower.
_ALL_DEV_TOOLS = (
    "search", "read_file", "run_shell", "fetch_url",
    "write_file", "edit", "git_commit", "git_push",
    "create_child_ticket", "post_comment", "set_status", "retain_fact",
    "related_tickets", "graph_neighbors", "kubectl_read", "read_claude_memory",
)
_PLANNER_TOOLS = (
    "search", "read_file", "run_shell", "fetch_url",
    "create_child_ticket", "post_comment", "set_status", "retain_fact",
    "related_tickets", "graph_neighbors", "kubectl_read", "mongo_query",
    "read_claude_memory",
)
_ARCHITECT_TOOLS = (
    "search", "read_file", "create_child_ticket", "post_comment", "set_status",
    "related_tickets", "read_claude_memory",
)
_FACT_TOOLS = (
    "search", "retain_fact", "post_comment", "set_status",
    "related_tickets", "read_claude_memory",
)


ROLES: dict[str, RoleConfig] = {
    "architect": RoleConfig(
        name="architect",
        model=CLAUDE_MODEL,
        transport="claude_cli",
        max_turns=6,
        tool_allowlist=_ARCHITECT_TOOLS,
    ),
    "sr_developer": RoleConfig(
        name="sr_developer",
        model="qwen3.6-35b-a3b",
        transport="openai",
        max_turns=25,
        tool_allowlist=_PLANNER_TOOLS,
    ),
    "developer": RoleConfig(
        name="developer",
        model="qwen3-coder-next",
        transport="openai",
        max_turns=40,
        tool_allowlist=_ALL_DEV_TOOLS,
    ),
    "fact_extract": RoleConfig(
        # Using already-downloaded thinking-tuned 4B model. gemma-3-4b-it is
        # not in the LM Studio catalogue as-named; qwen3-4b-thinking-2507
        # is ~2.3 GB on disk, handles XML reflection cleanly.
        name="fact_extract",
        model="qwen/qwen3-4b-thinking-2507",
        transport="openai",
        max_turns=4,
        tool_allowlist=_FACT_TOOLS,
    ),
}


def role(name: str) -> RoleConfig:
    try:
        return ROLES[name]
    except KeyError as exc:
        raise KeyError(f"unknown role {name!r}; valid: {list(ROLES)}") from exc
