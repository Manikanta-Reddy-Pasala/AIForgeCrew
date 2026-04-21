"""Single-tick orchestrator.

Entry point:
    python -m aiforge_core.runtime <role>

Behaviour:
  1. Acquire a non-blocking per-role lock (/tmp/aiforge-tick-<role>.lock).
  2. Claim the oldest todo ticket for the role.
  3. Build the CONTEXT bundle (aiforge-deep-context CLI output) +
     events tail + role system prompt.
  4. Run the tool-loop against the role's LLM transport (LM Studio or
     claude CLI), capped by role.max_turns and TICK_MAX_WALL_SECS.
  5. Every tool call and LLM turn writes a ticket_event and a
     structured log line.
  6. Release lock, exit.
"""
from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from typing import Iterator

from . import tickets, roles as roles_mod, tools as tools_mod
from .config import (
    RoleConfig, TICK_MAX_TURNS, TICK_MAX_WALL_SECS,
    WORKTREE_ROOT, role as role_cfg_get,
)
from .llm import AssistantTurn, complete
from .logging_setup import emit, get_logger
from .tools import ToolContext


# ─────────────────────────── Locking ────────────────────────────────────
@contextmanager
def _role_lock(path: str) -> Iterator[bool]:
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.write(fd, f"{os.getpid()}\n".encode())
            yield True
        except BlockingIOError:
            yield False
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


# ─────────────────────────── Context bundle ─────────────────────────────
def _build_context_bundle(ticket_title: str, role: str) -> str:
    """Shells out to aiforge-deep-context CLI. Budgeted at 120s."""
    try:
        env = {**os.environ, "ROLE": role}
        proc = subprocess.run(
            ["/Users/manikanta/.local/bin/aiforge-deep-context", ticket_title],
            capture_output=True, text=True, timeout=150, env=env, check=False,
        )
        return proc.stdout or f"(deep-context empty; stderr={proc.stderr[:500]})"
    except subprocess.TimeoutExpired:
        return "(deep-context TIMEOUT — agent should rely on search tool)"
    except FileNotFoundError:
        return "(deep-context binary missing — agent should call search tool)"


def _format_events_tail(ticket_id: int, limit: int = 20) -> str:
    events = tickets.comments(ticket_id, limit=limit)
    if not events:
        return "(no prior events)"
    lines = []
    for e in events:
        ts = e["created_at"].strftime("%H:%M:%S") if e.get("created_at") else "?"
        kind = e.get("kind") or "?"
        role = e.get("agent_role") or "?"
        body = (e.get("body") or "").replace("\n", " ")[:500]
        lines.append(f"[{ts}] [{role}] ({kind}) {body}")
    return "\n".join(lines)


# ─────────────────────────── Worktree ───────────────────────────────────
def _slugify(s: str) -> str:
    import re
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s.lower()).strip("-")
    return s[:40] or "ticket"


def _ensure_branch_and_worktree(ticket: tickets.Ticket,
                                repo_guess: str = "AIForgeCrew") -> str | None:
    """Create `aiforge/ONE-<parent>-<slug>` branch and a dedicated
    worktree the first time we touch this parent-ticket tree. Children
    reuse the same branch/worktree via ticket.branch.

    Returns worktree absolute path, or None if repo inference fails.
    """
    parent_ident = ticket.identifier
    if ticket.parent_id:
        p = tickets.get(ticket.parent_id)
        if p is not None:
            parent_ident = p.identifier

    existing = ticket.branch
    # Branch derived from parent identifier + parent title slug.
    if existing:
        branch = existing
    else:
        parent = tickets.get(ticket.parent_id) if ticket.parent_id else ticket
        slug = _slugify(parent.title if parent else ticket.title)
        branch = f"aiforge/{parent_ident}-{slug}"

    # Infer repo path — look for repo name in the ticket body / context,
    # otherwise default to AIForgeCrew (safe for meta tickets).
    repo_name = _infer_repo_from_ticket(ticket) or repo_guess
    repo_dir = os.path.join(WORKTREE_ROOT, repo_name)
    if not os.path.isdir(os.path.join(repo_dir, ".git")):
        return None

    worktree_path = os.path.join(repo_dir, ".aiforge-worktrees", parent_ident)
    if not os.path.isdir(worktree_path):
        os.makedirs(os.path.dirname(worktree_path), exist_ok=True)
        # Create branch off origin/main if absent.
        subprocess.run(["git", "fetch", "origin"], cwd=repo_dir, check=False,
                       capture_output=True)
        subprocess.run(
            ["git", "worktree", "add", "-B", branch, worktree_path, "origin/main"],
            cwd=repo_dir, check=False, capture_output=True,
        )

    # Persist branch on ticket for re-use.
    if ticket.branch != branch:
        with tickets._conn() as c, c.cursor() as cur:
            cur.execute("UPDATE tickets SET branch=%s WHERE id=%s",
                        (branch, ticket.id))
            c.commit()
    return worktree_path


def _infer_repo_from_ticket(ticket: tickets.Ticket) -> str | None:
    text = f"{ticket.title}\n{ticket.body}"
    candidates = [
        "mongoEventListner", "PosClientBackend", "PosServerBackend",
        "MongoDbService", "PosService", "BusinessService", "PosFrontend",
        "PosAdmin", "PosPythonBackend", "PosDataSyncService", "Scheduler",
        "QuartzScheduler", "EmailService", "NotificationService",
        "AIForgeCrew",
    ]
    for name in candidates:
        if name in text:
            return name
    return None


# ─────────────────────────── Tool loop ──────────────────────────────────
def _run_tool_loop(role_cfg: RoleConfig, ticket: tickets.Ticket,
                   worktree_path: str | None, log) -> dict:
    ctx = ToolContext(
        role=role_cfg.name, ticket_id=ticket.id,
        ticket_identifier=ticket.identifier, parent_id=ticket.parent_id,
        worktree_path=worktree_path, logger=log,
    )
    tool_schemas = tools_mod.schemas(role_cfg.tool_allowlist)

    # Initial messages.
    ctx_bundle = _build_context_bundle(ticket.title, role_cfg.name)
    events_tail = _format_events_tail(ticket.id)
    messages = roles_mod.build_messages(
        role_cfg.name, ticket, ctx_bundle, events_tail,
    )
    emit(log, "context.built",
         bundle_chars=len(ctx_bundle), events_chars=len(events_tail))

    t_start = time.time()
    turn = 0
    max_turns = min(role_cfg.max_turns, TICK_MAX_TURNS)
    stop_reason = "max_turns"
    while turn < max_turns:
        if time.time() - t_start > TICK_MAX_WALL_SECS:
            stop_reason = "wall_timeout"
            break
        turn += 1

        t0 = time.time()
        try:
            turn_result: AssistantTurn = complete(
                role_cfg, messages, tool_schemas,
                timeout_s=min(300, TICK_MAX_WALL_SECS - int(time.time() - t_start)),
            )
        except Exception as exc:
            emit(log, "llm.error", turn=turn, error=str(exc)[:300])
            tickets.add_event(ticket.id, role_cfg.name, "error",
                              body=f"llm call failed: {exc}",
                              metadata={"turn": turn})
            stop_reason = "llm_error"
            break
        dt = round((time.time() - t0) * 1000)
        emit(log, "llm.turn", turn=turn, dur_ms=dt,
             finish_reason=turn_result.finish_reason,
             tokens_in=turn_result.prompt_tokens,
             tokens_out=turn_result.completion_tokens,
             tool_calls=len(turn_result.tool_calls or []))

        # Append assistant message to history (including tool_calls).
        assistant_msg: dict = {"role": "assistant",
                               "content": turn_result.content or ""}
        if turn_result.tool_calls:
            assistant_msg["tool_calls"] = turn_result.tool_calls
        messages.append(assistant_msg)

        tickets.add_event(ticket.id, role_cfg.name, "llm_turn",
                          body=(turn_result.content or "")[:4000],
                          metadata={"turn": turn,
                                    "tokens_in": turn_result.prompt_tokens,
                                    "tokens_out": turn_result.completion_tokens,
                                    "tool_calls": [tc["function"]["name"]
                                                   for tc in (turn_result.tool_calls or [])]})

        if not turn_result.tool_calls:
            # No tool calls → treat as final.
            stop_reason = "model_done"
            break

        # Dispatch each tool call, feed results back.
        for tc in turn_result.tool_calls:
            name = tc["function"]["name"]
            arguments = tc["function"].get("arguments", "{}")
            emit(log, "tool.call", turn=turn, tool=name,
                 args_preview=arguments[:200])
            result = tools_mod.dispatch(ctx, name, arguments)
            emit(log, "tool.result", turn=turn, tool=name,
                 ok=result.ok, dur_ms=(result.meta or {}).get("dur_ms"),
                 chars=len(result.output))
            tickets.add_event(
                ticket.id, role_cfg.name, "tool_call",
                body=f"{name}({arguments[:300]}) → {result.output[:1000]}",
                metadata={"tool": name, "ok": result.ok, **(result.meta or {})},
            )
            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id"),
                "name": name,
                "content": result.output[:8000],
            })

    return {
        "stop_reason": stop_reason, "turns": turn,
        "wall_s": round(time.time() - t_start, 2),
    }


# ─────────────────────────── Entry ──────────────────────────────────────
def tick(role_name: str) -> int:
    rc = role_cfg_get(role_name)
    log = get_logger(role_name)

    with _role_lock(rc.lock_path) as got:
        if not got:
            emit(log, "lock.skip")
            return 0

        ticket = tickets.claim_next(role_name)
        if ticket is None:
            emit(log, "tick.idle")
            return 0

        emit(log, "tick.start", ticket=ticket.identifier, title=ticket.title)
        try:
            tickets.update_status(ticket.id, "in_progress", role=role_name)
            worktree = _ensure_branch_and_worktree(ticket)
            emit(log, "worktree.prepared",
                 ticket=ticket.identifier, path=worktree)

            summary = _run_tool_loop(rc, ticket, worktree, log)
            emit(log, "tick.end", ticket=ticket.identifier, **summary)
        except Exception as exc:
            emit(log, "tick.exception", ticket=ticket.identifier,
                 error=str(exc)[:500])
            tickets.add_event(ticket.id, role_name, "error",
                              body=f"orchestrator exception: {exc}")
            # Leave status as in_progress so a human/Paperclip-janitor can
            # see — we don't auto-mark blocked anymore; that's what killed
            # good runs in v4.
            return 2
    return 0


def _cli():
    if len(sys.argv) < 2:
        print("usage: python -m aiforge_core.runtime <role>", file=sys.stderr)
        sys.exit(2)
    role = sys.argv[1]
    sys.exit(tick(role))


if __name__ == "__main__":
    _cli()
