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
from dataclasses import dataclass, field
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
    # Per-tick in-memory cache so repeated read_file on the same (path,
    # range) costs zero I/O and returns a short "cached — already in
    # your history" notice instead of the full file, nudging the model
    # to stop re-reading.
    read_cache: dict = field(default_factory=dict)
    # Parsed `## Files` allowlist from ticket body. Doer may only
    # write_file / edit paths that appear here (substring or basename
    # match). None = no allowlist parsed yet, empty list = parsed but
    # no files listed (refuse all writes). Populated by orchestrator
    # before the tool loop starts.
    allowed_files: list[str] | None = None


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
    if not query or not query.strip():
        return ToolResult(False, "search: query is empty",
                          {"error": "empty_query"})
    try:
        mem = memory.Memory()
        hits = mem.search(query, role=ctx.role, parent_id=None,
                          top_k=top_k, wing_prefix=wing_prefix)
    except Exception as exc:
        return ToolResult(False, f"search backend unavailable: {exc}",
                          {"error": "backend_failure"})
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
    "description": "Read a UTF-8 file IN FULL (default end_line=2000). Absolute or worktree-relative paths. If you call this twice on the same path within a tick, the second call returns a 'cached — already in your history' notice: stop re-reading and work from earlier output. For narrow-scope lookups use `grep_repo` first to locate the right line, then `read_file` once.",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "start_line": {"type": "integer", "default": 1},
            "end_line": {"type": "integer", "default": 2000},
        },
        "required": ["path"],
    },
})
def _tool_read_file(ctx: ToolContext, path: str, start_line: int = 1,
                    end_line: int = 2000) -> ToolResult:
    p = _resolve_path(ctx, path)
    cache_key = f"{p}:{start_line}:{end_line}"
    if cache_key in ctx.read_cache:
        return ToolResult(
            True,
            (f"(cached — you already read `{path}` earlier this tick. "
             f"Scroll up in your message history for the content. "
             f"Stop re-reading; proceed to write_file / edit / "
             f"post_comment with what you already know.)"),
            {"path": p, "cached": True},
        )
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return ToolResult(False, f"file not found: {p}", {})
    sliced = lines[max(0, start_line - 1): end_line]
    numbered = "".join(f"{i + start_line:5d}| {l}" for i, l in enumerate(sliced))
    ctx.read_cache[cache_key] = True
    return ToolResult(True, numbered or "(empty)",
                      {"path": p, "lines_returned": len(sliced)})


# ── grep_repo
@register("grep_repo", {
    "name": "grep_repo",
    "description": "ripgrep across the worktree (or a directory under it). Use this BEFORE read_file to locate the exact file:line you need. Example: `grep_repo('@RestController', '*.java')` returns every controller class with path:line. Much cheaper than reading whole files.",
    "parameters": {
        "type": "object",
        "properties": {
            "pattern": {"type": "string",
                        "description": "Regex pattern (ripgrep syntax). Case-insensitive by default."},
            "glob": {"type": "string",
                     "description": "File glob filter, e.g. '*.java', '*.py', '!*.test.js'. Optional."},
            "path": {"type": "string",
                     "description": "Subdir to search under worktree root. Optional; defaults to worktree."},
            "max_matches": {"type": "integer", "default": 60,
                            "description": "Cap results so output stays small."},
        },
        "required": ["pattern"],
    },
})
def _tool_grep_repo(ctx: ToolContext, pattern: str,
                    glob: str | None = None,
                    path: str | None = None,
                    max_matches: int = 60) -> ToolResult:
    base = ctx.worktree_path or WORKTREE_ROOT
    target = _resolve_path(ctx, path) if path else base
    cmd = ["rg", "-n", "-i", "--no-heading", "--color=never",
           "-S", "-m", str(int(max_matches))]
    if glob:
        cmd += ["-g", glob]
    cmd += [pattern, target]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, timeout=30, check=False,
        )
    except FileNotFoundError:
        return ToolResult(False, "rg (ripgrep) not installed; fall back to run_shell('grep -rn …')", {})
    except subprocess.TimeoutExpired:
        return ToolResult(False, "grep_repo timed out (30s) — narrow the pattern or glob", {})
    out = proc.stdout.decode("utf-8", "replace")
    err = proc.stderr.decode("utf-8", "replace")
    if proc.returncode == 1 and not out:
        return ToolResult(True, "(no matches)",
                          {"matches": 0, "pattern": pattern})
    if proc.returncode not in (0, 1):
        return ToolResult(False, f"rg exit={proc.returncode}: {err[:400]}", {})
    lines = out.splitlines()
    trimmed = "\n".join(lines[:max_matches])
    meta = {"matches": len(lines), "pattern": pattern,
            "truncated": len(lines) > max_matches}
    return ToolResult(True, trimmed or "(no matches)", meta)


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
    p = _resolve_path(ctx, path, for_write=True)
    if ctx.worktree_path:
        wt = os.path.abspath(ctx.worktree_path)
        if not (os.path.abspath(p).startswith(wt + os.sep) or os.path.abspath(p) == wt):
            return ToolResult(
                False,
                f"refused: write path {p} escapes worktree {wt}. "
                "Pass a repo-relative path like 'src/main/...' or "
                "an absolute path under the worktree.",
                {"worktree": wt, "requested": p},
            )
    refusal = _scope_check(ctx, p, path)
    if refusal:
        return refusal
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)
    return ToolResult(True, f"wrote {len(content)} bytes to {p}", {"path": p})


@register("edit", {
    "name": "edit",
    "description": "Replace an exact old_string with new_string in an existing file. old_string must match exactly once. Prefer this over write_file for small edits.",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old_string": {"type": "string"},
            "new_string": {"type": "string"},
        },
        "required": ["path", "old_string", "new_string"],
    },
})
def _tool_edit(ctx: ToolContext, path: str, old_string: str, new_string: str) -> ToolResult:
    p = _resolve_path(ctx, path, for_write=True)
    if ctx.worktree_path:
        wt = os.path.abspath(ctx.worktree_path)
        if not (os.path.abspath(p).startswith(wt + os.sep) or os.path.abspath(p) == wt):
            return ToolResult(
                False,
                f"refused: edit path {p} escapes worktree {wt}. "
                "Pass a repo-relative path.",
                {"worktree": wt, "requested": p},
            )
    refusal = _scope_check(ctx, p, path)
    if refusal:
        return refusal
    if not os.path.exists(p):
        return ToolResult(False, f"file not found: {p}", {})
    with open(p, "r", encoding="utf-8") as f:
        src = f.read()
    cnt = src.count(old_string)
    if cnt == 0:
        return ToolResult(False, f"old_string not found in {p}", {"matches": 0})
    if cnt > 1:
        return ToolResult(False, f"old_string matches {cnt}x in {p}; make it unique",
                          {"matches": cnt})
    new = src.replace(old_string, new_string, 1)
    with open(p, "w", encoding="utf-8") as f:
        f.write(new)
    return ToolResult(True, f"edited {p} (-{len(old_string)} +{len(new_string)} chars)",
                      {"path": p})


# ── run_shell (no allowlist — user authorised full shell)
_DANGEROUS_SHELL = (
    "git reset --hard",
    "git push --force",
    "git push -f",
    "git clean -fdx",
    "git clean -fd",
    "git commit --allow-empty",
    "git commit -a --allow-empty",
    "rm -rf /",
    "rm -rf ~",
    "rm -rf $HOME",
    "sudo ",
    ":(){",
    "mkfs",
    "dd if=",
)


def _reject_shell(command: str, ctx: ToolContext) -> str | None:
    """Return rejection reason if command must be blocked, else None."""
    low = command.strip().lower()
    for bad in _DANGEROUS_SHELL:
        if bad in low:
            return f"refused: dangerous pattern `{bad.strip()}` in command"
    # cd escape: if the command starts with `cd /path && ...` and the
    # path is outside the worktree, block. Doer's ONE-2 post-mortem
    # showed gpt-oss-20b cd'ing into AIForgeCrew itself and running
    # destructive git ops there.
    if ctx.worktree_path:
        stripped = command.strip()
        if stripped.startswith("cd "):
            try:
                target = stripped.split(" ", 1)[1].split("&&", 1)[0].strip()
                target = target.strip("'").strip('"').rstrip("/")
                if target.startswith("~"):
                    target = os.path.expanduser(target)
                abs_target = os.path.abspath(target) if target else ""
                wt = os.path.abspath(ctx.worktree_path)
                if abs_target and not (abs_target.startswith(wt + os.sep)
                                       or abs_target == wt):
                    return (f"refused: `cd {target}` escapes worktree {wt}. "
                            "Run commands in the current worktree only. "
                            "Remove the `cd …` prefix — the shell already "
                            "starts in the worktree.")
            except Exception:
                pass
    return None


@register("run_shell", {
    "name": "run_shell",
    "description": "Run a shell command in the ticket worktree. Output truncated to 8 KB. 120s timeout. Destructive patterns (git reset --hard, git push -f, git clean -fd, git commit --allow-empty, rm -rf /, sudo, …) and `cd` escapes from the worktree are REFUSED.",
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
    # Safety: block destructive patterns + cd escapes from worktree.
    refusal = _reject_shell(command, ctx)
    if refusal:
        return ToolResult(False, refusal, {"blocked": True})
    # cwd must exist, else subprocess raises FileNotFoundError at spawn.
    cwd = ctx.worktree_path if (ctx.worktree_path and os.path.isdir(ctx.worktree_path)) \
          else os.path.expanduser("~")
    try:
        proc = subprocess.run(
            ["bash", "-lc", command], cwd=cwd,
            capture_output=True, timeout=timeout_s, check=False,
        )
    except subprocess.TimeoutExpired:
        return ToolResult(False, f"timeout after {timeout_s}s", {"error": "timeout"})
    except Exception as e:
        return ToolResult(False, f"shell spawn failed: {e}",
                          {"error": type(e).__name__})
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


# ── Ticket ops
@register("create_child_ticket", {
    "name": "create_child_ticket",
    "description": "Create a child ticket under the current ticket. REQUIRED: `project` (the target repo folder under ~/codeRepo such as 'PosClientBackend', 'PosServerBackend', 'MongoDbService', etc.) — the orchestrator creates the git worktree based on this. Dedup-safe across project. Optional `max_turns` overrides the assignee role's default turn budget: 20=trivial, 40=normal, 60=docs, 100=comprehensive README, 150=deep analysis.",
    "parameters": {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "body": {"type": "string"},
            "project": {"type": "string",
                        "description": "Target repo folder under ~/codeRepo (e.g. 'PosClientBackend'). REQUIRED for doer children. Planner/feedback children inherit parent.project if omitted."},
            "assignee_role": {"type": "string",
                              "enum": ["planner", "doer", "feedback", "learner"],
                              "default": "doer"},
            "priority": {"type": "string", "enum": ["low", "medium", "high", "urgent"], "default": "medium"},
            "max_turns": {"type": "integer",
                          "description": "Turn budget. 20=trivial, 40=normal, 60=docs, 100=comprehensive README, 150=deep analysis. Max 300.",
                          "minimum": 4, "maximum": 300},
        },
        "required": ["title", "body"],
    },
})
def _tool_create_child(ctx: ToolContext, title: str, body: str,
                       assignee_role: str | None = None,
                       priority: str = "medium",
                       max_turns: int | None = None,
                       project: str | None = None) -> ToolResult:
    parent = tickets.get(ctx.ticket_id)
    if parent is None:
        return ToolResult(False, "parent ticket missing", {})
    # Default assignee to doer when agent omits (common with smaller models).
    if not assignee_role or assignee_role.strip() in ("", "null", "None"):
        assignee_role = "doer"
    # Resolve project — explicit param > parent.project.
    eff_project = (project or "").strip() or parent.project
    # Enforce doer-ticket body structure + project assignment.
    if assignee_role == "doer":
        if not eff_project or eff_project.strip().lower() in ("aiforgecrew", ""):
            return ToolResult(
                False,
                ("doer child REQUIRES `project` — the repo folder under "
                 "~/codeRepo (e.g. 'PosClientBackend', 'PosServerBackend', "
                 "'MongoDbService'). Orchestrator uses this to create the "
                 "git worktree. AIForgeCrew is the orchestrator itself and "
                 "cannot be a target."),
                {"missing": "project"},
            )
        lowered = body.lower()
        required = ("## scope", "## files", "## acceptance")
        missing = [s for s in required if s not in lowered]
        if missing:
            return ToolResult(
                False,
                ("doer child body missing required sections: "
                 f"{', '.join(missing)}. Every doer ticket MUST have "
                 "`## Scope`, `## Files` (with ≤3 file:line anchors), "
                 "and `## Acceptance` sections. Re-emit with these."),
                {"missing_sections": missing},
            )
    # Dedup across the project, not just direct siblings — Planner otherwise
    # spawns same-title README tickets under different parents.
    needle = title.strip().lower()
    siblings = tickets.children(ctx.ticket_id)
    project_dupes = tickets.by_title_project(title, parent.project)
    for existing in list(siblings) + list(project_dupes):
        if existing.title.strip().lower() == needle and existing.id != ctx.ticket_id:
            return ToolResult(True,
                              f"ticket with same title already exists: {existing.identifier}",
                              {"child_identifier": existing.identifier,
                               "deduped": True})
    child = tickets.create(
        title=title, body=body,
        assignee_role=assignee_role,
        parent_id=ctx.ticket_id,
        priority=priority,
        branch=parent.branch,    # share branch
        project=eff_project,
        metadata={"created_by_role": ctx.role,
                  **({"max_turns": int(max_turns)} if max_turns else {})},
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
    # Auto-route doer → feedback. Doer's in_review means "I'm done editing";
    # Feedback verifies before the ticket lands truly in_review. Keeps the
    # prompt sequence "commit → post_comment → set_status(in_review)" working
    # without the ticket skipping the review gate.
    canonical = {"developer": "doer"}.get(ctx.role, ctx.role)
    if status == "in_review" and canonical == "doer":
        import json as _json
        patch = {"last_note": note, "routed_to_feedback_by": ctx.role,
                 "routed_to_feedback_at": time.strftime(
                     "%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        with tickets._conn() as c, c.cursor() as cur:
            cur.execute(
                "UPDATE tickets SET assignee_role='feedback', status='todo', "
                "completed_at=NULL, "
                "metadata = metadata || %s::jsonb WHERE id=%s",
                (_json.dumps({k: v for k, v in patch.items() if v is not None}),
                 ctx.ticket_id),
            )
            c.commit()
        tickets.add_event(
            ctx.ticket_id, ctx.role, "routing",
            body="→ feedback (auto-routed from doer set_status)",
            metadata={"new_assignee": "feedback", "trigger": "doer_set_in_review"},
        )
        return ToolResult(True,
                          "routed to feedback (doer in_review → feedback todo)",
                          {"status": "todo", "assignee_role": "feedback"})

    tickets.update_status(ctx.ticket_id, status, role=ctx.role,
                          metadata_patch={"last_note": note} if note else None)
    return ToolResult(True, f"status → {status}", {"status": status})


# ── update_assignee (supervisor only — triage + re-route)
@register("update_assignee", {
    "name": "update_assignee",
    "description": "Re-route this ticket to a different role. Use after triage: pick 'planner' for multi-step work, 'doer' for trivial single-commit edits, 'learner' for post-merge fact distillation. Also lets you set priority + labels + project.",
    "parameters": {
        "type": "object",
        "properties": {
            "assignee_role": {"type": "string",
                              "enum": ["planner", "doer", "feedback", "learner",
                                       "supervisor"]},
            "priority": {"type": "string",
                         "enum": ["low", "medium", "high", "urgent"]},
            "project": {"type": "string"},
            "labels": {"type": "array", "items": {"type": "string"}},
            "reason": {"type": "string",
                       "description": "1-sentence why — stored in metadata.supervisor_decision"},
        },
        "required": ["assignee_role", "reason"],
    },
})
def _tool_update_assignee(ctx: ToolContext, assignee_role: str, reason: str,
                          priority: str | None = None,
                          project: str | None = None,
                          labels: list[str] | None = None) -> ToolResult:
    # Force status=todo so the new assignee's tick will claim it. Prevents
    # the supervisor's set_status(in_review) from stranding the ticket.
    sets: list[str] = ["assignee_role=%s", "status='todo'", "completed_at=NULL"]
    params: list[Any] = [assignee_role]
    if priority:
        sets.append("priority=%s"); params.append(priority)
    if project:
        sets.append("project=%s"); params.append(project)
    if labels is not None:
        sets.append("labels=%s"); params.append(labels)
    patch = {
        "supervisor_decision": reason,
        "supervisor_decided_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    import json as _json
    sets.append("metadata = metadata || %s::jsonb")
    params.append(_json.dumps(patch))
    params.append(ctx.ticket_id)
    with tickets._conn() as c, c.cursor() as cur:
        cur.execute(f"UPDATE tickets SET {', '.join(sets)} WHERE id=%s", params)
        c.commit()
    # Audit event
    tickets.add_event(
        ctx.ticket_id, ctx.role, "routing",
        body=f"→ {assignee_role}: {reason}",
        metadata={"new_assignee": assignee_role, "priority": priority,
                  "project": project, "labels": labels, "reason": reason},
    )
    # Decision-trace memory for future supervisor consistency
    try:
        t = tickets.get(ctx.ticket_id)
        if t is not None:
            mem = memory.Memory()
            mem.retain_fact(
                text=f"Routed {t.identifier} ({t.title[:80]}) → {assignee_role}. Reason: {reason[:300]}",
                tier="t3", wing="decisions/supervisor",
                source=f"supervisor@{t.identifier}",
                metadata={"ticket": t.identifier, "assignee": assignee_role,
                          "priority": priority, "reason": reason[:500]},
            )
    except Exception:
        pass  # Decision trace is best-effort
    return ToolResult(True, f"routed to {assignee_role}",
                      {"assignee_role": assignee_role})


# ── Feedback verdicts
@register("verdict_pass", {
    "name": "verdict_pass",
    "description": "Pass the Doer's work. Requires test_output evidence. Triggers: ticket status → in_review + Learner auto-queued.",
    "parameters": {
        "type": "object",
        "properties": {
            "test_output": {"type": "string",
                            "description": "excerpt of passing test / compile output that justifies the pass"},
            "note": {"type": "string", "description": "short human-readable summary"},
        },
        "required": ["test_output", "note"],
    },
})
def _tool_verdict_pass(ctx: ToolContext, test_output: str, note: str) -> ToolResult:
    if len(test_output.strip()) < 40:
        return ToolResult(False,
                          "verdict_pass requires test_output ≥ 40 chars (cite actual evidence)",
                          {"error": "insufficient_evidence"})
    tickets.add_event(
        ctx.ticket_id, ctx.role, "verdict",
        body=f"PASS: {note}\n\nEvidence:\n{test_output[:2000]}",
        metadata={"verdict": "pass", "note": note},
    )
    tickets.update_status(
        ctx.ticket_id, "in_review", role=ctx.role,
        metadata_patch={"feedback_verdict": "pass",
                        "feedback_note": note[:500]},
    )
    # Queue a Learner ticket with a pre-built DIGEST so learner doesn't
    # have to hunt for data. Two cases:
    #   - Current ticket has a parent: learner is a sibling under that parent.
    #   - Current ticket is top-level: learner becomes a CHILD of this ticket.
    # Dedup-safe either way: skip if a learner ticket already exists.
    try:
        t = tickets.get(ctx.ticket_id)
        if t is not None:
            parent_id_for_learner = t.parent_id if t.parent_id else t.id
            cohort = tickets.children(parent_id_for_learner)
            already = any(
                s.assignee_role in ("learner", "fact_extract")
                for s in cohort
            )
            if not already:
                parent = tickets.get(t.parent_id) if t.parent_id else t
                digest = _build_learner_digest(t, parent)
                learner = tickets.create(
                    title=f"Distil facts: {parent.title[:50] if parent else t.identifier}",
                    body=digest,
                    assignee_role="learner",
                    parent_id=parent_id_for_learner,
                    priority="low",
                    branch=t.branch,
                    project=t.project,
                    metadata={"auto_queued_by": "feedback.verdict_pass",
                              "trigger_ticket": t.identifier,
                              "digest_chars": len(digest)},
                )
                tickets.add_event(
                    ctx.ticket_id, ctx.role, "learner_queued",
                    body=f"→ {learner.identifier} (digest {len(digest)} chars)",
                    metadata={"learner_ticket": learner.identifier,
                              "digest_chars": len(digest)},
                )
    except Exception as exc:
        import logging
        logging.getLogger("aiforge.tools").warning(
            "verdict_pass: learner queue failed: %s", exc,
        )
    return ToolResult(True, "verdict=pass; status→in_review; learner queued",
                      {"verdict": "pass"})


@register("verdict_fail", {
    "name": "verdict_fail",
    "description": "Fail the Doer's work. Lists concrete fixes. Ticket goes back to doer with the fixlist attached as guidance.",
    "parameters": {
        "type": "object",
        "properties": {
            "fixlist": {"type": "array", "items": {"type": "string"},
                        "description": "3-7 concrete fixes the Doer must apply. Each bullet references a file:line or test name."},
            "note": {"type": "string", "description": "one-paragraph summary"},
        },
        "required": ["fixlist", "note"],
    },
})
def _tool_verdict_fail(ctx: ToolContext, fixlist: list[str], note: str) -> ToolResult:
    if not fixlist or len(fixlist) < 1:
        return ToolResult(False, "verdict_fail requires ≥1 fix item", {})
    body = f"FAIL: {note}\n\nFixlist:\n" + "\n".join(f"  - {f}" for f in fixlist[:7])
    tickets.add_event(
        ctx.ticket_id, ctx.role, "verdict",
        body=body, metadata={"verdict": "fail", "note": note, "fixlist": fixlist},
    )
    # Send back to doer for another pass.
    import json as _json
    with tickets._conn() as c, c.cursor() as cur:
        cur.execute(
            "UPDATE tickets SET assignee_role='doer', status='todo', "
            "metadata = metadata || %s::jsonb WHERE id=%s",
            (_json.dumps({"feedback_verdict": "fail",
                           "feedback_fixlist": fixlist,
                           "feedback_note": note[:500]}),
             ctx.ticket_id),
        )
        c.commit()
    return ToolResult(True, "verdict=fail; sent back to doer",
                      {"verdict": "fail", "fixes": len(fixlist)})


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
    # Dedup: if an existing fact is ≥0.85 similar and shares the same wing, skip.
    try:
        hits = mem.search(query=text[:240], top_k=5)
        for h in hits or []:
            h_wing = (getattr(h, "wing", None)
                      or (getattr(h, "metadata", {}) or {}).get("wing"))
            if h_wing != wing:
                continue
            score = getattr(h, "score", None) or getattr(h, "rerank", 0.0) or 0.0
            if score >= 0.85:
                tickets.add_event(
                    ctx.ticket_id, ctx.role, "retain_skipped",
                    body=f"dedup hit id={getattr(h,'id',None)} score={score:.2f}",
                    metadata={"memory_id": getattr(h, "id", None), "tier": tier, "wing": wing},
                )
                return ToolResult(
                    True,
                    f"skipped (duplicate of memory id={getattr(h,'id',None)}, score={score:.2f})",
                    {"deduped": True, "memory_id": getattr(h, "id", None)},
                )
    except Exception:
        pass  # search outage must not block retain
    rid = mem.retain_fact(
        text=text, tier=tier, wing=wing,
        source=f"{ctx.role}@{ctx.ticket_identifier}",
        metadata={
            "ticket": ctx.ticket_identifier,
            "role": ctx.role,
            "retained_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "hit_count": 0,
        },
    )
    tickets.add_event(ctx.ticket_id, ctx.role, "retain",
                      body=text, metadata={"memory_id": rid, "tier": tier, "wing": wing})
    return ToolResult(True, f"retained memory id={rid} ({tier}/{wing})",
                      {"memory_id": rid})


# ── related_tickets (find similar tickets by embedding)
@register("related_tickets", {
    "name": "related_tickets",
    "description": "Find tickets similar to the current one (or a free-text query) by embedding similarity. Returns identifier + title + status + snippet.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "optional — defaults to the current ticket's title+body"},
            "top_k": {"type": "integer", "default": 5},
        },
    },
})
def _tool_related_tickets(ctx: ToolContext, query: str | None = None,
                          top_k: int = 5) -> ToolResult:
    t = tickets.get(ctx.ticket_id)
    if t is None:
        return ToolResult(False, "current ticket not found", {})
    q = query or f"{t.title}\n{t.body[:2000]}"
    mem = memory.Memory()
    # Look for ticket-wing memories first (T1 episodic we auto-write on finalize),
    # then fall back to cross-tier similarity.
    hits = mem.search(q, role=ctx.role, top_k=max(top_k * 3, 15))
    seen: set[str] = set()
    rows: list[str] = []
    for h in hits:
        wing = (h.metadata or {}).get("wing", "")
        if not wing.startswith("ticket/"):
            continue
        ident = wing.split("/", 1)[1]
        if ident == t.identifier or ident in seen:
            continue
        seen.add(ident)
        rel = tickets.get(ident)
        if rel is None:
            continue
        rows.append(
            f"  {rel.identifier:<8}  {rel.status:<12}  {rel.assignee_role or '-':<14}  "
            f"{rel.title[:60]}"
        )
        if len(rows) >= top_k:
            break
    if not rows:
        return ToolResult(True, "(no related tickets yet — T1 memory is still warming up)",
                          {"count": 0})
    return ToolResult(True, "\n".join(rows), {"count": len(rows)})


# ── graph_neighbors (read graphify-out/graph.json for file-level neighbours)
@register("graph_neighbors", {
    "name": "graph_neighbors",
    "description": "Return files in the code knowledge graph that reference OR are referenced by the given file. Works only for repos indexed by graphify. Good first call when you want to see a call-site map.",
    "parameters": {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "repo-relative path, e.g. 'src/main/java/.../Foo.java'"},
            "depth": {"type": "integer", "default": 1, "description": "1 = direct neighbours; 2 = 2-hop"},
            "limit": {"type": "integer", "default": 20},
        },
        "required": ["file_path"],
    },
})
def _tool_graph_neighbors(ctx: ToolContext, file_path: str, depth: int = 1,
                          limit: int = 20) -> ToolResult:
    # find repo root (walk up the worktree looking for graphify-out/graph.json)
    base = ctx.worktree_path or WORKTREE_ROOT
    # worktrees live at <repo>/.aiforge-worktrees/<PARENT>; graphify-out at <repo> root.
    repo_root = base
    for _ in range(4):
        cand = os.path.join(repo_root, "graphify-out", "graph.json")
        if os.path.isfile(cand):
            graph_path = cand
            break
        parent = os.path.dirname(repo_root)
        if parent == repo_root:
            return ToolResult(False, "graph.json not found walking up from worktree", {})
        repo_root = parent
    else:
        return ToolResult(False, "no graphify-out/graph.json within 4 levels", {})

    try:
        with open(graph_path, "r", encoding="utf-8") as f:
            graph = json.load(f)
    except Exception as e:
        return ToolResult(False, f"graph.json load failed: {e}", {})

    nodes = graph.get("nodes") or []
    edges = graph.get("edges") or []
    # normalise target path
    target = file_path
    if target.startswith(repo_root):
        target = target[len(repo_root):].lstrip("/")

    def _match(n: dict) -> bool:
        p = n.get("path") or n.get("file") or n.get("id") or ""
        return p.endswith(target) or target in p

    hit_ids = {n.get("id") for n in nodes if _match(n)}
    if not hit_ids:
        return ToolResult(True, f"(no graph node matches {target!r})", {"count": 0})

    frontier = set(hit_ids)
    neighbours: set[str] = set()
    for _ in range(max(1, depth)):
        new_neighbours = set()
        for e in edges:
            src, dst = e.get("source") or e.get("from"), e.get("target") or e.get("to")
            if src in frontier and dst not in hit_ids and dst not in neighbours:
                new_neighbours.add(dst)
            if dst in frontier and src not in hit_ids and src not in neighbours:
                new_neighbours.add(src)
        if not new_neighbours:
            break
        neighbours |= new_neighbours
        frontier = new_neighbours

    id_to_path = {n.get("id"): (n.get("path") or n.get("file") or n.get("id"))
                  for n in nodes}
    rows = sorted({id_to_path.get(nid, nid) for nid in neighbours if id_to_path.get(nid)})
    if not rows:
        return ToolResult(True, f"no neighbours found for {target}", {"count": 0})
    return ToolResult(True, "\n".join(rows[:limit]),
                      {"count": min(len(rows), limit), "total": len(rows),
                       "graph": graph_path})


# ── kubectl_read (safe subset: get/describe/logs/top only; TLS skip enforced)
_KUBECTL_ALLOWED_VERBS = {"get", "describe", "logs", "top", "version", "api-resources",
                          "cluster-info", "config"}
_KUBECTL_FORBIDDEN = {"apply", "delete", "patch", "edit", "replace", "scale",
                      "rollout", "exec", "cp", "create", "run", "port-forward",
                      "proxy", "drain", "cordon", "uncordon", "taint"}


@register("kubectl_read", {
    "name": "kubectl_read",
    "description": "Run a READ-ONLY kubectl command (get/describe/logs/top) against the OneShell cluster. Auto-appends --insecure-skip-tls-verify. Forbidden: apply/delete/patch/exec/run/port-forward.",
    "parameters": {
        "type": "object",
        "properties": {
            "args": {"type": "string",
                     "description": "args after 'kubectl', e.g. 'get pods -n pos' or 'logs deployment/posclientbackend -n pos --tail=200'"},
            "timeout_s": {"type": "integer", "default": 60},
        },
        "required": ["args"],
    },
})
def _tool_kubectl_read(ctx: ToolContext, args: str, timeout_s: int = 60) -> ToolResult:
    try:
        parts = shlex.split(args)
    except ValueError as e:
        return ToolResult(False, f"could not parse args: {e}", {})
    if not parts:
        return ToolResult(False, "empty args", {})
    verb = parts[0]
    if verb in _KUBECTL_FORBIDDEN:
        return ToolResult(False, f"forbidden verb {verb!r}; kubectl_read is read-only",
                          {"error": "forbidden"})
    if verb not in _KUBECTL_ALLOWED_VERBS:
        return ToolResult(False,
                          f"verb {verb!r} not allowlisted; use: {sorted(_KUBECTL_ALLOWED_VERBS)}",
                          {"error": "not_allowed"})
    cmd = ["kubectl", *parts, "--insecure-skip-tls-verify"]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout_s, check=False)
    except subprocess.TimeoutExpired:
        return ToolResult(False, f"kubectl timeout after {timeout_s}s", {"error": "timeout"})
    except FileNotFoundError:
        return ToolResult(False, "kubectl binary missing on this host", {"error": "no_kubectl"})
    out = (proc.stdout + proc.stderr).decode("utf-8", "replace")[:10_240]
    return ToolResult(proc.returncode == 0,
                      f"exit={proc.returncode}\n{out}",
                      {"exit_code": proc.returncode})


# ── mongo_query (read-only find/aggregate against sharded cluster)
_MONGO_READ_OPS = {"find", "findOne", "countDocuments", "aggregate", "distinct",
                   "stats", "listCollections"}
_MONGO_DEFAULT_NS = "mongodb"
_MONGO_DEFAULT_POD = "prod-cluster-mongos-0"
_MONGO_DEFAULT_DB = "oneshell"
_MONGO_URI = os.environ.get(
    "AIFORGE_MONGO_URI",
    "mongodb://databaseAdmin:akyFqNelEclMhlkNx06c@localhost:27017/oneshell?authSource=admin",
)


@register("mongo_query", {
    "name": "mongo_query",
    "description": "Run a READ-ONLY mongosh query against prod MongoDB via kubectl exec of the mongos pod. Operations: find, findOne, countDocuments, aggregate, distinct, stats. Arguments are a mongosh snippet expression. 60s timeout. 10KB output cap.",
    "parameters": {
        "type": "object",
        "properties": {
            "collection": {"type": "string",
                           "description": "e.g. 'changeStreamEventErrors', 'productTxn', 'sales'"},
            "operation": {"type": "string", "enum": sorted(list(_MONGO_READ_OPS))},
            "query_expr": {"type": "string",
                           "description": "the expression given to mongosh, e.g. '{resolved:false}' for find, or '[{$match:{...}},{$limit:5}]' for aggregate"},
            "limit": {"type": "integer", "default": 20},
            "db": {"type": "string", "default": _MONGO_DEFAULT_DB},
        },
        "required": ["collection", "operation", "query_expr"],
    },
})
def _tool_mongo_query(ctx: ToolContext, collection: str, operation: str,
                      query_expr: str, limit: int = 20,
                      db: str = _MONGO_DEFAULT_DB) -> ToolResult:
    if operation not in _MONGO_READ_OPS:
        return ToolResult(False, f"op {operation!r} not allowed (read-only tool)",
                          {"error": "not_allowed"})
    # crude guard against injected write ops inside the expression
    bad_tokens = ("insertOne", "insertMany", "updateOne", "updateMany",
                  "deleteOne", "deleteMany", "drop(", "remove(", "replaceOne",
                  "$out", "$merge", "bulkWrite", "renameCollection")
    if any(tok in query_expr for tok in bad_tokens):
        return ToolResult(False, "expression contains a write-op token; rejected",
                          {"error": "write_in_expr"})
    if operation == "aggregate":
        expr = f"db.{collection}.aggregate({query_expr}).toArray().slice(0, {limit})"
    elif operation == "find":
        expr = (f"db.{collection}.find({query_expr}).limit({limit}).toArray()")
    elif operation == "findOne":
        expr = f"db.{collection}.findOne({query_expr})"
    elif operation == "countDocuments":
        expr = f"db.{collection}.countDocuments({query_expr})"
    elif operation == "distinct":
        expr = f"db.{collection}.distinct({query_expr})"
    elif operation == "stats":
        expr = f"db.{collection}.stats()"
    elif operation == "listCollections":
        expr = "db.getCollectionNames()"
    script = f"use {db}; printjson({expr})"
    cmd = [
        "kubectl", "exec", "-n", _MONGO_DEFAULT_NS, _MONGO_DEFAULT_POD,
        "--insecure-skip-tls-verify", "--",
        "mongosh", _MONGO_URI, "--quiet", "--eval", script,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=60, check=False)
    except subprocess.TimeoutExpired:
        return ToolResult(False, "mongo_query timeout after 60s", {"error": "timeout"})
    except FileNotFoundError:
        return ToolResult(False, "kubectl missing on this host", {"error": "no_kubectl"})
    out = (proc.stdout + proc.stderr).decode("utf-8", "replace")[:10_240]
    return ToolResult(proc.returncode == 0,
                      f"exit={proc.returncode}\n{out}",
                      {"exit_code": proc.returncode, "op": operation,
                       "collection": collection})


# ── read_claude_memory (agent consultation of human's ~/.claude/memory/*.md)
_CLAUDE_MEMORY_DIR = os.environ.get(
    "AIFORGE_CLAUDE_MEMORY_DIR",
    os.path.expanduser("~/.claude/memory"),
)


@register("read_claude_memory", {
    "name": "read_claude_memory",
    "description": "Read the human operator's personal claude-memory markdown index. These files capture domain notes, decisions, and SOPs that are NOT in the repo. Use when you need business/operator context the code alone doesn't explain.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "grep-style pattern across all memory files"},
            "file": {"type": "string", "description": "optional — read a specific file by name"},
            "limit": {"type": "integer", "default": 40, "description": "max matching lines"},
        },
    },
})
def _tool_read_claude_memory(ctx: ToolContext, query: str | None = None,
                             file: str | None = None, limit: int = 40) -> ToolResult:
    if not os.path.isdir(_CLAUDE_MEMORY_DIR):
        return ToolResult(False, f"claude-memory dir not present: {_CLAUDE_MEMORY_DIR}",
                          {"error": "no_dir"})
    if file:
        path = os.path.join(_CLAUDE_MEMORY_DIR, file)
        if not os.path.isfile(path):
            return ToolResult(False, f"claude-memory file not found: {file}", {})
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            body = f.read(12_288)
        return ToolResult(True, body, {"path": path})
    if not query:
        return ToolResult(False, "either 'query' or 'file' required", {"error": "bad_args"})
    # cheap grep across .md files, return ranked lines
    import re
    pat = re.compile(query, re.IGNORECASE)
    hits: list[str] = []
    for fn in sorted(os.listdir(_CLAUDE_MEMORY_DIR), reverse=True):
        if not fn.endswith(".md"):
            continue
        path = os.path.join(_CLAUDE_MEMORY_DIR, fn)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for i, line in enumerate(f, 1):
                    if pat.search(line):
                        hits.append(f"{fn}:{i}: {line.rstrip()[:200]}")
                        if len(hits) >= limit:
                            break
        except Exception:
            continue
        if len(hits) >= limit:
            break
    if not hits:
        return ToolResult(True, f"(no claude-memory hits for {query!r})", {"count": 0})
    return ToolResult(True, "\n".join(hits), {"count": len(hits)})


def _build_learner_digest(trigger_ticket, parent_ticket) -> str:
    """Build a complete self-contained DIGEST for the Learner ticket body.

    Includes:
      - Parent (or trigger) ticket title + body
      - All sibling tickets with assignee, status, last comment, commits
      - Trigger's own verdict_pass note + test_output
    The Learner then has everything it needs to emit retain_fact calls
    without reading files from the repo.
    """
    from . import tickets as tk

    lines: list[str] = []
    scope_id = parent_ticket.id if parent_ticket else trigger_ticket.id
    cohort = tk.children(scope_id)

    # Parent section
    lines.append("# LEARNER DIGEST")
    lines.append("")
    lines.append(f"Triggered by: **{trigger_ticket.identifier}** "
                 f"(feedback verdict_pass)")
    lines.append("")
    if parent_ticket and parent_ticket.id != trigger_ticket.id:
        lines.append(f"## PARENT — {parent_ticket.identifier}")
        lines.append(f"**Title:** {parent_ticket.title}")
        body = (parent_ticket.body or "").strip()[:1500]
        if body:
            lines.append("")
            lines.append(body)
    else:
        lines.append(f"## TICKET — {trigger_ticket.identifier}")
        lines.append(f"**Title:** {trigger_ticket.title}")
        body = (trigger_ticket.body or "").strip()[:1500]
        if body:
            lines.append("")
            lines.append(body)

    # Sibling / child summary
    if cohort:
        lines.append("")
        lines.append(f"## CHILDREN / SIBLINGS ({len(cohort)})")
        for c in cohort:
            if c.id == trigger_ticket.id:
                marker = " ← TRIGGER"
            else:
                marker = ""
            lines.append(f"- **{c.identifier}**  {c.status}  "
                         f"assignee={c.assignee_role}{marker}  —  "
                         f"{c.title[:80]}")
            # Last comment from this ticket's events
            last_comment = _last_comment(c.id)
            if last_comment:
                lines.append(f"    last comment: {last_comment[:300]}")
            # Commits from tool_call events
            commits = _commits_from_events(c.id)
            if commits:
                lines.append(f"    commits: {', '.join(commits[:3])}")
            # Files touched
            files = _files_touched_from_events(c.id)
            if files:
                lines.append(f"    files: {', '.join(files[:5])}")

    # Trigger's own verdict evidence
    verdict_ev = _last_event_by_kind(trigger_ticket.id, "verdict")
    if verdict_ev:
        lines.append("")
        lines.append("## FEEDBACK VERDICT")
        lines.append(verdict_ev[:1500])

    # Instructions (concise — full spec is in LEARNER_SYSTEM prompt)
    lines.append("")
    lines.append("## YOUR TASK")
    lines.append(
        "Emit up to 5 `retain_fact` calls from the content above. Each fact:\n"
        "- ≤ 300 chars, anchored to a file:line or commit sha from above,\n"
        "- wing = `skills/<service>` / `patterns/<topic>` / `rules/<area>`,\n"
        "- call `search(fact_text[:60])` first to avoid duplicates.\n"
        "Then `post_comment` with a bulleted list of stored facts + `set_status(done)`."
    )
    return "\n".join(lines)


def _last_comment(ticket_id: int) -> str:
    """Return the most recent post_comment body for ticket_id, or empty."""
    try:
        import psycopg
        from psycopg.rows import dict_row
        from .config import AIFORGE_DSN
        with psycopg.connect(AIFORGE_DSN, row_factory=dict_row,
                             connect_timeout=3) as c, c.cursor() as cur:
            cur.execute(
                "SELECT body FROM ticket_events WHERE ticket_id=%s "
                "AND kind='comment' ORDER BY created_at DESC LIMIT 1",
                (ticket_id,),
            )
            row = cur.fetchone()
            return (row["body"] or "").strip() if row else ""
    except Exception:
        return ""


def _last_event_by_kind(ticket_id: int, kind: str) -> str:
    try:
        import psycopg
        from psycopg.rows import dict_row
        from .config import AIFORGE_DSN
        with psycopg.connect(AIFORGE_DSN, row_factory=dict_row,
                             connect_timeout=3) as c, c.cursor() as cur:
            cur.execute(
                "SELECT body FROM ticket_events WHERE ticket_id=%s "
                "AND kind=%s ORDER BY created_at DESC LIMIT 1",
                (ticket_id, kind),
            )
            row = cur.fetchone()
            return (row["body"] or "").strip() if row else ""
    except Exception:
        return ""


def _commits_from_events(ticket_id: int) -> list[str]:
    """Extract commit shas from git_commit tool_call events."""
    try:
        import psycopg
        from psycopg.rows import dict_row
        from .config import AIFORGE_DSN
        with psycopg.connect(AIFORGE_DSN, row_factory=dict_row,
                             connect_timeout=3) as c, c.cursor() as cur:
            cur.execute(
                "SELECT body FROM ticket_events WHERE ticket_id=%s "
                "AND kind='tool_call' AND body LIKE 'git_commit%%' "
                "ORDER BY created_at ASC",
                (ticket_id,),
            )
            rows = cur.fetchall()
    except Exception:
        return []
    import re
    shas: list[str] = []
    pat = re.compile(r"\b([0-9a-f]{7,40})\b")
    for r in rows:
        m = pat.search((r["body"] or "").lower())
        if m:
            shas.append(m.group(1)[:12])
    return list(dict.fromkeys(shas))  # dedup preserving order


def _files_touched_from_events(ticket_id: int) -> list[str]:
    """Extract file paths from edit / write_file tool_call args."""
    try:
        import psycopg
        from psycopg.rows import dict_row
        from .config import AIFORGE_DSN
        with psycopg.connect(AIFORGE_DSN, row_factory=dict_row,
                             connect_timeout=3) as c, c.cursor() as cur:
            cur.execute(
                "SELECT body FROM ticket_events WHERE ticket_id=%s "
                "AND kind='tool_call' AND "
                "(body LIKE 'edit(%%' OR body LIKE 'write_file(%%')",
                (ticket_id,),
            )
            rows = cur.fetchall()
    except Exception:
        return []
    import re
    pat = re.compile(r'"path"\s*:\s*"([^"]+)"')
    files: list[str] = []
    for r in rows:
        m = pat.search(r["body"] or "")
        if m:
            files.append(m.group(1))
    return list(dict.fromkeys(files))


# ─────────────────────────── helpers ────────────────────────────────────
def parse_allowed_files(body: str) -> list[str] | None:
    """Extract the `## Files` section from a ticket body and return a list
    of path patterns. Returns None if no `## Files` header found (caller
    should treat as 'no scope constraint' — legacy tickets). Returns []
    if header present but empty (refuse all writes — malformed ticket).

    Accepts several formats:
      - "- src/foo/Bar.java:45"
      - "- src/foo/Bar.java (adds isActive field)"
      - "* src/foo/Bar.java"
      - bare path on its own line

    Reads only the first `## Files` section, stops at next `## ` or blank
    line separator.
    """
    if not body:
        return None
    lower = body.lower()
    marker_idx = lower.find("## files")
    if marker_idx < 0:
        return None
    # Move past the header line
    nl = body.find("\n", marker_idx)
    if nl < 0:
        return []
    section = body[nl + 1:]
    # Stop at next `## ` heading
    end = section.find("\n## ")
    if end >= 0:
        section = section[:end]
    paths: list[str] = []
    import re as _re
    for line in section.splitlines():
        s = line.strip().lstrip("-*").lstrip()
        if not s:
            continue
        if s.startswith("#"):
            break
        # Capture first path-ish token: contains `/` and optional `:<digits>`
        m = _re.match(r"`?([\w./-]+\.[A-Za-z0-9]+|[\w./-]+/[\w./-]+)(?::\d+)?", s)
        if m:
            paths.append(m.group(1).strip("`"))
    return paths


def _scope_check(ctx: ToolContext, abs_path: str, agent_path: str) -> ToolResult | None:
    """Refuse writes that escape the ticket's `## Files` allowlist.

    - Supervisors/Planners/Learners/Feedback never write code; they won't
      call write/edit anyway, but their allowed_files is None → skip.
    - Doer: allowed_files parsed from ticket body. Match by basename OR
      by suffix (so 'src/main/java/.../Foo.java' allows both the full
      path and 'Foo.java').
    - allowed_files == [] (empty list): refuse all writes — malformed
      ticket with no files declared.
    """
    if ctx.role != "doer":
        return None  # non-doers aren't subject to scope gate
    allow = ctx.allowed_files
    if allow is None:
        return None  # legacy ticket, no ## Files section — no gate
    if not allow:
        return ToolResult(
            False,
            ("refused: ticket has `## Files` header but lists no paths. "
             "Ask Planner to re-emit the ticket with concrete file:line "
             "anchors under `## Files` (or escalate via update_assignee)."),
            {"scope_creep": True, "requested": agent_path},
        )
    ap = os.path.abspath(abs_path)
    base = os.path.basename(ap)
    for entry in allow:
        e = entry.strip()
        if not e:
            continue
        # Accept full substring match OR basename match.
        if e in ap or ap.endswith(e) or base == os.path.basename(e):
            return None
    return ToolResult(
        False,
        (f"refused: `{agent_path}` is not in the ticket's `## Files` "
         f"allowlist. Allowed: {', '.join(allow[:5])}"
         f"{'…' if len(allow) > 5 else ''}. "
         "If this file is legitimately needed, escalate via "
         "update_assignee(assignee_role='planner', labels=['doer-blocked'], "
         "reason='need <file> added to ## Files')."),
        {"scope_creep": True, "requested": agent_path,
         "allowed_files": allow},
    )


def _resolve_path(ctx: ToolContext, path: str, *, for_write: bool = False) -> str:
    """Resolve agent-supplied paths.

    Read path order:
      1. Absolute path (return as-is if it exists).
      2. Worktree-relative: <worktree>/<path>.
      3. WORKTREE_ROOT-relative: ~/codeRepo/<path>  (handles paths that
         start with the repo name, e.g. 'mongoEventListner/src/…').
      4. Stripped-repo-prefix: if worktree is inside <repo> and the
         agent-supplied path starts with '<repo>/', drop that prefix.

    Write path (`for_write=True`): if the agent passes an absolute path
    that escapes the worktree (e.g. /Users/…/codeRepo/PosServerBackend/…
    when the worktree is the aiforge branch worktree), strip the repo
    root and remap to <worktree>/<relative>. This prevents "wrote bytes
    but git_commit saw nothing" — the root bug behind ONE-209 loop.
    """
    if os.path.isabs(path):
        if for_write and ctx.worktree_path:
            wt = os.path.abspath(ctx.worktree_path)
            abspath = os.path.abspath(path)
            if abspath.startswith(wt + os.sep) or abspath == wt:
                return abspath
            # Try to remap: strip any ~/codeRepo/<repo>/ prefix and rejoin under worktree.
            root = os.path.abspath(WORKTREE_ROOT) + os.sep
            if abspath.startswith(root):
                tail = abspath[len(root):]
                # tail looks like "PosServerBackend/src/main/..."; drop first segment
                parts = tail.split(os.sep, 1)
                if len(parts) == 2:
                    remapped = os.path.abspath(os.path.join(wt, parts[1]))
                    return remapped
            # Cannot remap safely — return as-is; caller will error.
            return abspath
        return path
    candidates: list[str] = []
    base = ctx.worktree_path or WORKTREE_ROOT
    candidates.append(os.path.abspath(os.path.join(base, path)))

    # try absolute under WORKTREE_ROOT (~/codeRepo)
    candidates.append(os.path.abspath(os.path.join(WORKTREE_ROOT, path)))

    # strip duplicated repo-name prefix if worktree is under a repo named X
    # and path starts with 'X/...'
    if ctx.worktree_path:
        parts = path.split("/", 1)
        if len(parts) == 2:
            repo_name = parts[0]
            if f"/codeRepo/{repo_name}/" in ctx.worktree_path + "/":
                candidates.append(os.path.abspath(
                    os.path.join(ctx.worktree_path, parts[1])))

    for c in candidates:
        if os.path.exists(c):
            return c
    return candidates[0]
