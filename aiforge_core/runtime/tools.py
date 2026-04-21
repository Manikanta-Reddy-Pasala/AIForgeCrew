"""Tool catalogue exposed to the LLM.

Each function is defined once. The dispatcher:
  - builds an OpenAI-compatible JSON schema (for `tools` param).
  - dispatches tool_calls by name, with structured error + timing.
  - writes each call as a ticket_event(kind='tool_call', …) for replay.

Tools are role-whitelisted via `config.RoleConfig.tool_allowlist`. The
runtime trims `schemas()` output to the allowlist before sending to the
LLM so the model never sees a tool it can't use.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from . import tickets, memory
from .config import WORKTREE_ROOT, EMBED_SIDECAR_URL


# ─────────────────────────── Context per tick ───────────────────────────
@dataclass
class ToolContext:
    role: str
    ticket_id: int
    ticket_identifier: str
    parent_id: int | None
    worktree_path: str | None            # absolute path to git worktree
    logger: Any                          # logging.Logger (emit() + info())


# ─────────────────────────── Dispatch result ────────────────────────────
@dataclass
class ToolResult:
    ok: bool
    output: str
    meta: dict


# ─────────────────────────── Tool registry ──────────────────────────────
_REGISTRY: dict[str, tuple[Callable[..., ToolResult], dict]] = {}


def register(name: str, schema: dict):
    def deco(fn):
        _REGISTRY[name] = (fn, schema)
        return fn
    return deco


def schemas(allowlist: tuple[str, ...]) -> list[dict]:
    out = []
    for name in allowlist:
        if name not in _REGISTRY:
            continue
        _, schema = _REGISTRY[name]
        out.append({"type": "function", "function": schema})
    return out


def dispatch(ctx: ToolContext, name: str, arguments_json: str) -> ToolResult:
    if name not in _REGISTRY:
        return ToolResult(False, f"unknown tool {name}", {"error": "unknown_tool"})
    fn, _ = _REGISTRY[name]
    try:
        args = json.loads(arguments_json) if arguments_json else {}
    except json.JSONDecodeError as e:
        return ToolResult(False, f"invalid JSON args: {e}", {"error": "bad_args"})

    t0 = time.time()
    try:
        result = fn(ctx, **args)
    except TypeError as e:
        return ToolResult(False, f"tool {name} arg error: {e}", {"error": "bad_args"})
    except Exception as e:
        return ToolResult(False, f"tool {name} failed: {type(e).__name__}: {e}",
                          {"error": type(e).__name__})
    dur = round((time.time() - t0) * 1000)
    if isinstance(result, ToolResult):
        result.meta = {"dur_ms": dur, **(result.meta or {})}
        return result
    return ToolResult(True, str(result), {"dur_ms": dur})


# ═══════════════════════════ TOOLS ═══════════════════════════════════════
# Each @register call provides the JSON schema Claude/qwen see. Keep
# descriptions short + actionable — they show up in every prompt.


# ── search (memory, all tiers + claude-md + graphify via SearchResult)
@register("search", {
    "name": "search",
    "description": "Search all memory tiers (T1 ticket, T2 rules, T3 skills, T4 code + claude-memory) with bge-m3 + rerank. Returns ranked hits.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "wing_prefix": {"type": "string", "description": "optional: 'code/', 'rules/', 'skills/'"},
            "top_k": {"type": "integer", "default": 10},
        },
        "required": ["query"],
    },
})
def _tool_search(ctx: ToolContext, query: str, wing_prefix: str | None = None,
                 top_k: int = 10) -> ToolResult:
    mem = memory.Memory()
    hits = mem.search(query, role=ctx.role, parent_id=None,
                      top_k=top_k, wing_prefix=wing_prefix)
    if not hits:
        return ToolResult(True, "(no hits)", {"count": 0})
    lines = []
    for h in hits:
        src = h.source or h.metadata.get("path", "?")
        repo = h.metadata.get("repo", "?")
        lines.append(f"[{h.tier}] [{repo}] {src}\n  {h.text[:350].strip()}")
    return ToolResult(True, "\n".join(lines), {"count": len(hits)})


# ── read_file
@register("read_file", {
    "name": "read_file",
    "description": "Read a UTF-8 file. Supports line ranges. Absolute or worktree-relative paths.",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "start_line": {"type": "integer", "default": 1},
            "end_line": {"type": "integer", "default": 400},
        },
        "required": ["path"],
    },
})
def _tool_read_file(ctx: ToolContext, path: str, start_line: int = 1,
                    end_line: int = 400) -> ToolResult:
    p = _resolve_path(ctx, path)
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return ToolResult(False, f"file not found: {p}", {})
    sliced = lines[max(0, start_line - 1): end_line]
    numbered = "".join(f"{i + start_line:5d}| {l}" for i, l in enumerate(sliced))
    return ToolResult(True, numbered or "(empty)",
                      {"path": p, "lines_returned": len(sliced)})


# ── write_file
@register("write_file", {
    "name": "write_file",
    "description": "Create or overwrite a text file in the worktree. Use with care — always inside the ticket branch.",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
    },
})
def _tool_write_file(ctx: ToolContext, path: str, content: str) -> ToolResult:
    p = _resolve_path(ctx, path)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)
    return ToolResult(True, f"wrote {len(content)} bytes to {p}", {"path": p})


# ── run_shell (no allowlist — user authorised full shell)
@register("run_shell", {
    "name": "run_shell",
    "description": "Run an arbitrary shell command in the ticket worktree. Output truncated to 8 KB. 120s timeout.",
    "parameters": {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "timeout_s": {"type": "integer", "default": 120},
        },
        "required": ["command"],
    },
})
def _tool_run_shell(ctx: ToolContext, command: str, timeout_s: int = 120) -> ToolResult:
    cwd = ctx.worktree_path or os.getcwd()
    try:
        proc = subprocess.run(
            ["bash", "-lc", command], cwd=cwd,
            capture_output=True, timeout=timeout_s, check=False,
        )
    except subprocess.TimeoutExpired:
        return ToolResult(False, f"timeout after {timeout_s}s", {"error": "timeout"})
    out = (proc.stdout + proc.stderr).decode("utf-8", "replace")[:8192]
    return ToolResult(
        proc.returncode == 0,
        f"exit={proc.returncode}\n{out}",
        {"exit_code": proc.returncode},
    )


# ── fetch_url (full web, no allowlist — user authorised)
@register("fetch_url", {
    "name": "fetch_url",
    "description": "GET any URL. Returns first 12 KB of the body. 20s timeout.",
    "parameters": {
        "type": "object",
        "properties": {"url": {"type": "string"}},
        "required": ["url"],
    },
})
def _tool_fetch_url(ctx: ToolContext, url: str) -> ToolResult:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "aiforge-agent/5"})
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read(12_288).decode("utf-8", "replace")
            status = r.getcode()
    except Exception as e:
        return ToolResult(False, f"fetch failed: {e}", {"error": type(e).__name__})
    return ToolResult(True, f"HTTP {status}\n{body}", {"status": status, "url": url})


# ── git ops
@register("git_commit", {
    "name": "git_commit",
    "description": "Stage all changes in the worktree and create a commit. Branch must already exist (orchestrator creates it per parent ticket).",
    "parameters": {
        "type": "object",
        "properties": {"message": {"type": "string"}},
        "required": ["message"],
    },
})
def _tool_git_commit(ctx: ToolContext, message: str) -> ToolResult:
    cwd = ctx.worktree_path
    if not cwd:
        return ToolResult(False, "no worktree for this ticket", {})
    prefixed = f"{message}\n\nRef: {ctx.ticket_identifier}"
    subprocess.run(["git", "add", "-A"], cwd=cwd, check=False)
    proc = subprocess.run(
        ["git", "commit", "-m", prefixed], cwd=cwd, capture_output=True, check=False,
    )
    out = (proc.stdout + proc.stderr).decode("utf-8", "replace")
    return ToolResult(proc.returncode == 0, out,
                      {"exit_code": proc.returncode})


@register("git_push", {
    "name": "git_push",
    "description": "Push the current branch to origin (sets upstream).",
    "parameters": {"type": "object", "properties": {}},
})
def _tool_git_push(ctx: ToolContext) -> ToolResult:
    cwd = ctx.worktree_path
    if not cwd:
        return ToolResult(False, "no worktree", {})
    proc = subprocess.run(
        ["git", "push", "-u", "origin", "HEAD"], cwd=cwd,
        capture_output=True, check=False,
    )
    out = (proc.stdout + proc.stderr).decode("utf-8", "replace")
    return ToolResult(proc.returncode == 0, out, {"exit_code": proc.returncode})


# ── Paperclip-replacement ticket ops
@register("create_child_ticket", {
    "name": "create_child_ticket",
    "description": "Create a child ticket under the current ticket. Assign to the role that should implement it.",
    "parameters": {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "body": {"type": "string"},
            "assignee_role": {"type": "string", "enum": ["sr_developer", "developer", "fact_extract"]},
            "priority": {"type": "string", "enum": ["low", "medium", "high", "urgent"], "default": "medium"},
        },
        "required": ["title", "body", "assignee_role"],
    },
})
def _tool_create_child(ctx: ToolContext, title: str, body: str,
                       assignee_role: str, priority: str = "medium") -> ToolResult:
    parent = tickets.get(ctx.ticket_id)
    if parent is None:
        return ToolResult(False, "parent ticket missing", {})
    child = tickets.create(
        title=title, body=body,
        assignee_role=assignee_role,
        parent_id=ctx.ticket_id,
        priority=priority,
        branch=parent.branch,    # share branch
        project=parent.project,
        metadata={"created_by_role": ctx.role},
    )
    tickets.add_event(ctx.ticket_id, ctx.role, "child_created",
                      body=f"{child.identifier} → {assignee_role}",
                      metadata={"child_id": child.id, "child_identifier": child.identifier})
    return ToolResult(True, f"created {child.identifier} assignee={assignee_role}",
                      {"child_identifier": child.identifier})


@register("post_comment", {
    "name": "post_comment",
    "description": "Post a comment on the current ticket. This is how you deliver your analysis, plan, or status update.",
    "parameters": {
        "type": "object",
        "properties": {"body": {"type": "string"}},
        "required": ["body"],
    },
})
def _tool_post_comment(ctx: ToolContext, body: str) -> ToolResult:
    event_id = tickets.add_comment(ctx.ticket_id, ctx.role, body)
    return ToolResult(True, f"comment posted (event={event_id})",
                      {"event_id": event_id, "chars": len(body)})


@register("set_status", {
    "name": "set_status",
    "description": "Update the current ticket's status. Use 'done' when finished, 'in_review' to hand back to the parent agent, 'blocked' if truly stuck.",
    "parameters": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["in_progress", "in_review", "done", "blocked"]},
            "note": {"type": "string"},
        },
        "required": ["status"],
    },
})
def _tool_set_status(ctx: ToolContext, status: str, note: str | None = None) -> ToolResult:
    tickets.update_status(ctx.ticket_id, status, role=ctx.role,
                          metadata_patch={"last_note": note} if note else None)
    return ToolResult(True, f"status → {status}", {"status": status})


# ── retain_fact (write to memories)
@register("retain_fact", {
    "name": "retain_fact",
    "description": "Persist a net-new convention, constraint, anti-pattern, or recipe into durable memory. Must include a file:line or doc-path anchor.",
    "parameters": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "<=300 chars, one fact, anchored"},
            "tier": {"type": "string", "enum": ["t2", "t3"], "default": "t3"},
            "wing": {"type": "string", "description": "e.g. 'rules/canon', 'skills/mongoEventListner', 'patterns/kafka'"},
        },
        "required": ["text", "wing"],
    },
})
def _tool_retain(ctx: ToolContext, text: str, wing: str, tier: str = "t3") -> ToolResult:
    mem = memory.Memory()
    rid = mem.retain_fact(text=text, tier=tier, wing=wing,
                          source=f"{ctx.role}@{ctx.ticket_identifier}",
                          metadata={"ticket": ctx.ticket_identifier, "role": ctx.role})
    tickets.add_event(ctx.ticket_id, ctx.role, "retain",
                      body=text, metadata={"memory_id": rid, "tier": tier, "wing": wing})
    return ToolResult(True, f"retained memory id={rid} ({tier}/{wing})",
                      {"memory_id": rid})


# ─────────────────────────── helpers ────────────────────────────────────
def _resolve_path(ctx: ToolContext, path: str) -> str:
    if os.path.isabs(path):
        return path
    base = ctx.worktree_path or WORKTREE_ROOT
    return os.path.abspath(os.path.join(base, path))
