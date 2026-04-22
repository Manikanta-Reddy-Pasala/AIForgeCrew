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
from datetime import datetime, timezone
from typing import Iterator

from psycopg.rows import dict_row

from . import tickets, roles as roles_mod, tools as tools_mod
from . import memguard
from .config import (
    RoleConfig, TICK_MAX_TURNS, TICK_MAX_WALL_SECS,
    WORKTREE_ROOT, role as role_cfg_get,
)
from .llm import AssistantTurn, complete
from .logging_setup import emit, get_logger
from .tools import ToolContext


# Orphan / retry policy. Tunable via env.
STALE_EVENT_SECS = int(os.environ.get("AIFORGE_STALE_EVENT_SECS", "300"))
MAX_RECLAIMS = int(os.environ.get("AIFORGE_MAX_RECLAIMS", "3"))


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


def _linked_tickets_block(ticket: tickets.Ticket) -> str:
    """Render parent + siblings + embedding-related tickets for the prompt."""
    out: list[str] = []

    # Parent
    if ticket.parent_id:
        parent = tickets.get(ticket.parent_id)
        if parent is not None:
            out.append("## PARENT")
            out.append(f"  {parent.identifier}  {parent.status}  {parent.title}")
            summary = (parent.body or "").strip().replace("\n", " ")[:300]
            if summary:
                out.append(f"    body: {summary}")

    # Siblings (same parent)
    if ticket.parent_id:
        sibs = [s for s in tickets.children(ticket.parent_id) if s.id != ticket.id]
        if sibs:
            out.append("")
            out.append(f"## SIBLINGS ({len(sibs)}) — same parent, shared branch")
            for s in sibs[:10]:
                out.append(
                    f"  {s.identifier:<8} {s.status:<12} {s.assignee_role or '-':<14}  "
                    f"{s.title[:70]}"
                )

    # Direct children of this ticket (for sr_dev picking up an in-review parent)
    kids = tickets.children(ticket.id)
    if kids:
        out.append("")
        out.append(f"## CHILDREN ({len(kids)})")
        for c in kids[:10]:
            out.append(
                f"  {c.identifier:<8} {c.status:<12} {c.assignee_role or '-':<14}  "
                f"{c.title[:70]}"
            )

    # Embedding-related tickets via T1 episodic wing
    try:
        from .memory import Memory
        m = Memory()
        q = f"{ticket.title}\n{(ticket.body or '')[:1500]}"
        hits = m.search(q, role="sr_developer", top_k=25)
        seen: set[str] = {ticket.identifier}
        rows: list[str] = []
        for h in hits:
            wing = (h.metadata or {}).get("wing", "") or ""
            if not wing.startswith("ticket/"):
                continue
            ident = wing.split("/", 1)[1]
            if ident in seen:
                continue
            seen.add(ident)
            related = tickets.get(ident)
            if related is None:
                continue
            rows.append(
                f"  {related.identifier:<8} {related.status:<12}  "
                f"{related.title[:70]}"
            )
            if len(rows) >= 5:
                break
        if rows:
            out.append("")
            out.append("## RELATED (embedding-similar, done elsewhere)")
            out.extend(rows)
    except Exception:
        # memory backend unreachable → skip silently, don't fail the tick
        pass

    return "\n".join(out) if out else ""


def _graph_hint(worktree_path: str | None) -> str:
    """If the worktree's repo has graphify-out/graph.json, mention it so the
    agent knows graph_neighbors is available here."""
    if not worktree_path:
        return ""
    repo_root = worktree_path
    for _ in range(4):
        if os.path.isfile(os.path.join(repo_root, "graphify-out", "graph.json")):
            return f"\n## GRAPH\n  `{repo_root}/graphify-out/graph.json` present. Call `graph_neighbors(file_path)` for call-site maps.\n"
        parent = os.path.dirname(repo_root)
        if parent == repo_root:
            return ""
        repo_root = parent
    return ""


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


def _ensure_branch_and_worktree(ticket: tickets.Ticket) -> str | None:
    """Create `aiforge/ONE-<parent>-<slug>` branch and a dedicated
    worktree the first time we touch this parent-ticket tree. Children
    reuse the same branch/worktree via ticket.branch.

    Returns worktree absolute path, or None if a target repo can't be
    safely identified. NEVER silently falls back to the AIForgeCrew
    orchestrator repo — doing so caused ONE-2 to write junk into our
    own source tree.
    """
    # Walk to ROOT parent for worktree naming. For ONE-3 → ONE-4 → ONE-5
    # chain, all three share the worktree at .aiforge-worktrees/ONE-3.
    # The earlier one-level walk broke nested children — ONE-5 tried to
    # create a second worktree on the already-checked-out branch, failing.
    root = ticket
    while root.parent_id:
        p = tickets.get(root.parent_id)
        if p is None:
            break
        root = p
    parent_ident = root.identifier

    existing = ticket.branch
    # Branch derived from parent identifier + parent title slug.
    if existing:
        branch = existing
    else:
        parent = tickets.get(ticket.parent_id) if ticket.parent_id else ticket
        slug = _slugify(parent.title if parent else ticket.title)
        branch = f"aiforge/{parent_ident}-{slug}"

    # Infer repo path — prefer the ticket's own project field, then walk
    # up to the root. _infer_repo_from_ticket already checks .project
    # first, then scans title+body. Try child first (it may have its
    # own project), fall through to root parent if child has none.
    repo_name = _infer_repo_from_ticket(ticket) or \
                _infer_repo_from_ticket(root)
    if not repo_name:
        # No target repo could be identified. Refuse to create a
        # worktree — the tick will post a helpful comment and block.
        # Better than silently editing AIForgeCrew.
        return None
    # Hard rule: never run a ticket in the orchestrator's own source.
    if repo_name == "AIForgeCrew":
        return None
    repo_dir = os.path.join(WORKTREE_ROOT, repo_name)
    if not os.path.isdir(os.path.join(repo_dir, ".git")):
        return None

    worktree_path = os.path.join(repo_dir, ".aiforge-worktrees", parent_ident)
    if not os.path.isdir(worktree_path):
        os.makedirs(os.path.dirname(worktree_path), exist_ok=True)
        subprocess.run(["git", "fetch", "origin"], cwd=repo_dir, check=False,
                       capture_output=True)
        # Detect the repo's default branch dynamically (master vs main).
        default_branch = _detect_default_branch(repo_dir)
        base = f"origin/{default_branch}"
        proc = subprocess.run(
            ["git", "worktree", "add", "-B", branch, worktree_path, base],
            cwd=repo_dir, check=False, capture_output=True,
        )
        if proc.returncode != 0 or not os.path.isdir(worktree_path):
            # Worktree add failed. Do NOT fall back to repo_dir — that
            # would let the agent write directly to main working tree
            # (branch, uncommitted state, etc.). Block the ticket.
            err = (proc.stderr or b"").decode("utf-8", "replace")[:500]
            print(f"[worktree.failed] repo={repo_name} err={err}", flush=True)
            return None

    # Persist branch on ticket for re-use.
    if ticket.branch != branch:
        with tickets._conn() as c, c.cursor() as cur:
            cur.execute("UPDATE tickets SET branch=%s WHERE id=%s",
                        (branch, ticket.id))
            c.commit()
    return worktree_path


def _detect_default_branch(repo_dir: str) -> str:
    """Return the repo's default branch name ('main' or 'master' etc)."""
    # Prefer `origin/HEAD` if set
    proc = subprocess.run(
        ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
        cwd=repo_dir, check=False, capture_output=True,
    )
    if proc.returncode == 0:
        # stdout e.g. "refs/remotes/origin/master"
        ref = proc.stdout.decode("utf-8", "replace").strip()
        if "/" in ref:
            return ref.rsplit("/", 1)[1]
    # Fallback: try main, then master
    for candidate in ("main", "master"):
        probe = subprocess.run(
            ["git", "rev-parse", "--verify", f"origin/{candidate}"],
            cwd=repo_dir, check=False, capture_output=True,
        )
        if probe.returncode == 0:
            return candidate
    return "master"


_FORBIDDEN_REPOS = {"AIForgeCrew"}  # orchestrator's own source — never a target


def _infer_repo_from_ticket(ticket: tickets.Ticket) -> str | None:
    """Pick the repo directory under WORKTREE_ROOT whose name appears in
    title+body. Longest-match wins (so 'oneshell-commons-model' picks up
    oneshell-commons root, and 'MongoDbService' beats 'Mongo').

    Skips the orchestrator's own source repo even if someone clones it
    under WORKTREE_ROOT — belt-and-braces defence alongside the
    hard-refuse in _ensure_branch_and_worktree.
    """
    text = f"{ticket.title}\n{ticket.body}"
    project = (ticket.project or "").strip()
    if project and project not in _FORBIDDEN_REPOS and \
            os.path.isdir(os.path.join(WORKTREE_ROOT, project)):
        return project
    # Candidates sorted longest-first so substring collisions resolve correctly.
    try:
        all_dirs = [d for d in os.listdir(WORKTREE_ROOT)
                    if os.path.isdir(os.path.join(WORKTREE_ROOT, d))
                    and not d.startswith(".")
                    and d not in _FORBIDDEN_REPOS]
    except OSError:
        all_dirs = []
    all_dirs.sort(key=len, reverse=True)
    for name in all_dirs:
        if name in text:
            return name
    return None


def _compact_old_tool_results(messages: list[dict], keep_tail: int = 5) -> None:
    """In-place compact old tool result messages to prevent context bloat.

    Tool results (role='tool') older than the last `keep_tail` such messages
    get their content truncated to 400 chars + a `[elided]` marker. The
    assistant messages that referenced them stay intact so the model sees
    the tool-call chain — just not the full payload.

    Called after turn 15+ in the tool loop. Typical doer/planner tick hits
    this once ctx pressure starts mattering. Saves 50-80% of prompt tokens
    on long ticks without losing recent context.
    """
    # Collect indices of tool messages. Last `keep_tail` stay full.
    tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    if len(tool_indices) <= keep_tail:
        return
    to_trim = tool_indices[:-keep_tail]
    for i in to_trim:
        content = messages[i].get("content", "")
        if isinstance(content, str) and len(content) > 450:
            head = content[:400]
            elided = len(content) - len(head)
            messages[i]["content"] = (
                f"{head}\n…[{elided} chars elided — call the same tool again "
                f"if you need the full result]"
            )


# ─────────────────────────── Tool loop ──────────────────────────────────
def _run_tool_loop(role_cfg: RoleConfig, ticket: tickets.Ticket,
                   worktree_path: str | None, log) -> dict:
    allowed_files = tools_mod.parse_allowed_files(ticket.body or "")
    ctx = ToolContext(
        role=role_cfg.name, ticket_id=ticket.id,
        ticket_identifier=ticket.identifier, parent_id=ticket.parent_id,
        worktree_path=worktree_path, logger=log,
        allowed_files=allowed_files,
    )
    if allowed_files is not None:
        emit(log, "scope.allowed_files", count=len(allowed_files),
             files=allowed_files[:10])
    tool_schemas = tools_mod.schemas(role_cfg.tool_allowlist)

    # Initial messages. Context bundle varies by role:
    # - supervisor/feedback/learner: tiny models, short ctx — skip heavy
    #   deep-context CLI + embedding-related-tickets section. Feedback
    #   only needs the ticket body + doer's last comment + diff access
    #   (via read_file/run_shell).
    # - planner/doer: full bundle (deep-context + linked tickets + graph).
    canonical = _canonical_role(role_cfg.name)
    if canonical in ("feedback", "learner", "supervisor"):
        ctx_bundle = "(heavy context bundle skipped for this role — use `search` or `read_file` tools if you need specific files)"
        # Still show linked tickets (small, ≤ 20 lines).
        linked = _linked_tickets_block(ticket)
        if linked:
            ctx_bundle = f"{linked}\n\n{ctx_bundle}"
    else:
        ctx_bundle = _build_context_bundle(ticket.title, role_cfg.name)
        linked = _linked_tickets_block(ticket)
        if linked:
            ctx_bundle = f"{ctx_bundle}\n\n{linked}"
        g_hint = _graph_hint(worktree_path)
        if g_hint:
            ctx_bundle = f"{ctx_bundle}\n{g_hint}"
    events_tail = _format_events_tail(ticket.id)
    # Derive the repo name from the worktree path so the hint is accurate.
    worktree_repo = None
    if worktree_path and "/codeRepo/" in worktree_path:
        worktree_repo = worktree_path.split("/codeRepo/", 1)[1].split("/")[0]
    messages = roles_mod.build_messages(
        role_cfg.name, ticket, ctx_bundle, events_tail,
        worktree_path=worktree_path, worktree_repo=worktree_repo,
    )
    emit(log, "context.built",
         bundle_chars=len(ctx_bundle), events_chars=len(events_tail))

    t_start = time.time()
    turn = 0
    # Per-ticket override beats role default. Planner sets this via
    # create_child_ticket(max_turns=...) when it knows the workload size.
    # Caps against the global TICK_MAX_TURNS hard ceiling so a bad
    # metadata value can't runaway-eat the wall budget.
    _meta = ticket.metadata or {}
    _override = _meta.get("max_turns")
    try:
        _override = int(_override) if _override is not None else None
    except (TypeError, ValueError):
        _override = None
    _budget = _override if _override and _override > 0 else role_cfg.max_turns
    max_turns = min(_budget, TICK_MAX_TURNS)
    stop_reason = "max_turns"
    # Loop-guard: if agent repeats the same (tool, args) N times in a row,
    # we break out with a system nudge so it doesn't burn the wall budget.
    _recent_calls: list[str] = []
    # Semantic loop guard: per-path read counts + consecutive reads-only
    # turn counter. Catches models that re-read the same file in different
    # line ranges or read 15 files without ever writing.
    _path_reads: dict[str, int] = {}
    _consecutive_read_only_turns = 0
    _read_only_nudged = False
    _path_nudged: set[str] = set()
    _deadline_warned = False
    _has_commented = False
    _WRITE_TOOLS = {"write_file", "edit", "git_commit", "post_comment",
                    "retain_fact", "set_status", "verdict_pass",
                    "verdict_fail", "create_child_ticket", "update_assignee"}
    _READ_TOOLS = {"read_file", "search", "related_tickets",
                   "graph_neighbors", "run_shell", "fetch_url",
                   "kubectl_read", "mongo_query", "read_claude_memory"}
    while turn < max_turns:
        if time.time() - t_start > TICK_MAX_WALL_SECS:
            stop_reason = "wall_timeout"
            break
        turn += 1

        # Deadline watchdog — once we cross 75% of max_turns with no
        # post_comment yet, inject a hard nudge so the agent closes out.
        if (not _has_commented and not _deadline_warned
                and turn >= int(max_turns * 0.75)):
            _deadline_warned = True
            emit(log, "deadline.warn", turn=turn, max_turns=max_turns)
            messages.append({
                "role": "user",
                "content": (
                    f"⚠ DEADLINE WARNING: you are on turn {turn} of {max_turns}. "
                    "You have NOT posted a comment yet. In your VERY NEXT turn, "
                    "IN ORDER: (1) git_commit if you have uncommitted changes, "
                    "(2) post_comment with a brief summary of what you did, "
                    "(3) set_status(status='in_review'). Do not run any more "
                    "`ls`, `find`, `git log`, or `search` calls. Commit + "
                    "report + exit NOW."
                ),
            })

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
        # Rough messages size estimate (char count as proxy for tokens).
        msg_chars = sum(len(str(m.get("content", ""))) for m in messages) + sum(
            len(str(tc)) for m in messages
            for tc in (m.get("tool_calls") or [])
        )
        emit(log, "llm.turn", turn=turn, dur_ms=dt,
             finish_reason=turn_result.finish_reason,
             tokens_in=turn_result.prompt_tokens,
             tokens_out=turn_result.completion_tokens,
             tool_calls=len(turn_result.tool_calls or []),
             msg_chars=msg_chars,
             msg_count=len(messages))

        # Append assistant message to history (including tool_calls).
        assistant_msg: dict = {"role": "assistant",
                               "content": turn_result.content or ""}
        if turn_result.tool_calls:
            assistant_msg["tool_calls"] = turn_result.tool_calls
        messages.append(assistant_msg)

        # Context compaction: turn-based AND size-based.
        # Turn-based trigger catches slow bloat after turn 15.
        # Size-based trigger catches one huge read_file or shell output
        # that blows past the model's ctx immediately.
        # msg_chars is the cheap proxy we already computed above.
        _HARD_CAP = 60_000   # ~15k tokens rough; most role ctx is 16-128k
        if turn >= 15 or msg_chars > _HARD_CAP:
            _compact_old_tool_results(messages, keep_tail=5)
            # If STILL over hard cap after compaction, be more aggressive.
            recomputed = sum(len(str(m.get("content", ""))) for m in messages)
            if recomputed > _HARD_CAP:
                _compact_old_tool_results(messages, keep_tail=3)
                emit(log, "ctx.compact_aggressive",
                     turn=turn, before=msg_chars, after=recomputed)

        tickets.add_event(ticket.id, role_cfg.name, "llm_turn",
                          body=(turn_result.content or "")[:4000],
                          metadata={"turn": turn,
                                    "tokens_in": turn_result.prompt_tokens,
                                    "tokens_out": turn_result.completion_tokens,
                                    "tool_calls": [tc["function"]["name"]
                                                   for tc in (turn_result.tool_calls or [])]})

        if not turn_result.tool_calls:
            # Small models (qwen-coder, phi-mini, gpt-oss-20b) sometimes
            # emit a "thinking out loud" message ("Now let me write the
            # README...") then halt without actually calling a tool —
            # Doer burns 60 turns reading files, then vanishes.
            # Give up to 2 nudge-retries with escalating pressure, then
            # force-terminate with post_comment("stalled") + set_status.
            # Feedback's natural terminal (verdict_pass/fail) is a real
            # tool call, so this doesn't interfere there.
            if role_cfg.name in ("doer", "planner", "learner"):
                _nudge_count = locals().get("_nudge_count", 0)
                if _nudge_count < 2:
                    _nudge_count += 1
                    tickets.add_event(
                        ticket.id, role_cfg.name, "nudge",
                        body=f"model_done with no tool call — nudge {_nudge_count}/2",
                        metadata={"turn": turn, "nudge_n": _nudge_count},
                    )
                    if _nudge_count == 1:
                        nudge_msg = (
                            "You produced prose but no tool call. "
                            "EVERY turn must end with a tool call. "
                            "If you said you would write/edit a file, call "
                            "`write_file` or `edit` NOW. If analysis is done, "
                            "call `post_comment` with your findings then "
                            "`set_status(in_review)` (doer/planner) or "
                            "`set_status(done)` (learner)."
                        )
                    else:
                        nudge_msg = (
                            "FINAL WARNING. Still no tool call. "
                            "Call EXACTLY ONE of these now:\n"
                            "  • `post_comment(body=\"<what you have so far>\")` "
                            "    then `set_status(in_review)`\n"
                            "  • `create_child_ticket(...)` to split remaining work\n"
                            "  • `set_status(blocked)` with a note explaining why\n"
                            "No more prose-only turns — anything without a tool "
                            "call next will block the ticket."
                        )
                    messages.append({"role": "system", "content": nudge_msg})
                    continue
            stop_reason = "model_done"
            break

        # Dispatch each tool call, feed results back.
        looped = False
        _turn_saw_write = False
        _reread_warning: str | None = None
        for tc in turn_result.tool_calls:
            name = tc["function"]["name"]
            arguments = tc["function"].get("arguments", "{}")
            emit(log, "tool.call", turn=turn, tool=name,
                 args_preview=arguments[:200])

            # Loop-guard: same (tool,args) 3x consecutive → break out with
            # a system nudge appended to messages so next turn changes course.
            key = f"{name}::{arguments}"
            _recent_calls.append(key)
            _recent_calls[:] = _recent_calls[-3:]
            if len(_recent_calls) == 3 and len(set(_recent_calls)) == 1:
                emit(log, "loop.detected", turn=turn, tool=name)
                looped = True

            # Semantic loop: count reads of the same path. Catches
            # read_file(start=200,end=400) then read_file(start=400,end=600)
            # which the (tool,args) guard misses.
            if name == "read_file":
                try:
                    import json as _json
                    path = (_json.loads(arguments or "{}") or {}).get("path", "")
                except Exception:
                    path = ""
                if path:
                    _path_reads[path] = _path_reads.get(path, 0) + 1
                    if _path_reads[path] >= 3 and path not in _path_nudged:
                        _path_nudged.add(path)
                        _reread_warning = path

            if name in _WRITE_TOOLS:
                _turn_saw_write = True

            result = tools_mod.dispatch(ctx, name, arguments)
            if name == "post_comment" and result.ok:
                _has_commented = True
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

        # Track consecutive read-only turns and nudge if too long.
        if _turn_saw_write:
            _consecutive_read_only_turns = 0
        else:
            _consecutive_read_only_turns += 1

        if _reread_warning:
            tickets.add_event(ticket.id, role_cfg.name, "loop.reread",
                              body=f"path re-read 3+ times: {_reread_warning}",
                              metadata={"turn": turn, "path": _reread_warning})
            messages.append({
                "role": "user",
                "content": (
                    f"STOP re-reading `{_reread_warning}`. You've read it 3 "
                    "times already (possibly in different line ranges). "
                    "The full content is in your message history. "
                    "MOVE ON: write_file / edit the target, or post_comment "
                    "with your analysis, or set_status(in_review). "
                    "Do not call read_file on that path again this tick."
                ),
            })

        if _consecutive_read_only_turns >= 15 and not _read_only_nudged:
            _read_only_nudged = True
            tickets.add_event(ticket.id, role_cfg.name, "loop.read_only",
                              body=f"{_consecutive_read_only_turns} consecutive read-only turns",
                              metadata={"turn": turn})
            messages.append({
                "role": "user",
                "content": (
                    f"You've spent {_consecutive_read_only_turns} turns reading "
                    "with no write. That's enough exploration. The ticket's "
                    "`## Files` section names your target. Call `write_file` "
                    "or `edit` NOW with your best draft. You can revise on "
                    "the next turn. No more read_file / search / run_shell "
                    "until you have produced a write."
                ),
            })

        if looped:
            messages.append({
                "role": "user",
                "content": (
                    "STOP REPEATING that same tool call — you've run it 3 "
                    "times with identical args and the result isn't changing. "
                    "Change strategy NOW: (a) read_file on a specific path "
                    "from the CONTEXT bundle, (b) call post_comment with "
                    "what you already know + a `(speculative)` tag for "
                    "anything missing, or (c) call create_child_ticket for "
                    "the implementation and then set_status(status='in_review'). "
                    "Do not issue that same search again."
                ),
            })
            stop_reason = "loop_detected"
            # Give the model ONE more turn to recover before we hard-stop.
            _recent_calls.clear()
            if turn >= max_turns - 2:
                break

    return {
        "stop_reason": stop_reason, "turns": turn,
        "wall_s": round(time.time() - t_start, 2),
        "has_commented": _has_commented,
    }


# ─────────────────────────── Orphan reaper ──────────────────────────────
def _reap_orphans(role_name: str, log) -> int:
    """Reset or block tickets stuck in_progress for this role.

    Invoked under the per-role lock, so any in_progress ticket for this role
    is by definition orphaned (no other tick is running). The stale-event
    window guards against reaping a ticket whose tick died mid-update.

    Returns number of tickets acted on.
    """
    with tickets._conn() as c, c.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT t.*, "
            "  (SELECT MAX(created_at) FROM ticket_events "
            "    WHERE ticket_id=t.id) AS last_event_at "
            "FROM tickets t "
            "WHERE t.assignee_role=%s AND t.status='in_progress' "
            "ORDER BY t.id ASC",
            (role_name,),
        )
        rows = cur.fetchall()
    now = datetime.now(timezone.utc)
    acted = 0
    for r in rows:
        last = r.get("last_event_at") or r["created_at"]
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        age = (now - last).total_seconds()
        if age < STALE_EVENT_SECS:
            continue
        meta = dict(r.get("metadata") or {})
        count = int(meta.get("reclaim_count", 0))
        ident = r["identifier"]
        if count >= MAX_RECLAIMS:
            emit(log, "orphan.blocked",
                 ticket=ident, reclaims=count, stale_s=round(age, 1))
            tickets.add_event(
                r["id"], role_name, "system",
                body=f"orphan blocked after {count} reclaims "
                     f"(stale {int(age)}s)",
                metadata={"orphan": True, "reclaims": count},
            )
            tickets.update_status(
                r["id"], "blocked", role=role_name,
                metadata_patch={"last_blocked_reason": "max_reclaims"},
            )
        else:
            emit(log, "orphan.reclaimed",
                 ticket=ident, reclaims=count + 1, stale_s=round(age, 1))
            tickets.add_event(
                r["id"], role_name, "system",
                body=f"orphan reclaimed (stale {int(age)}s, "
                     f"attempt {count + 1}/{MAX_RECLAIMS})",
                metadata={"orphan": True, "reclaim_count": count + 1},
            )
            tickets.update_status(
                r["id"], "todo", role=role_name,
                metadata_patch={
                    "reclaim_count": count + 1,
                    "last_stop_reason": "orphan_reclaim",
                    "last_reclaim_at": now.isoformat(),
                },
            )
        acted += 1
    return acted


def _finalize_ticket(ticket: tickets.Ticket, role_name: str,
                     summary: dict, log) -> None:
    """Safety net: if the tool loop ended without the agent moving the
    ticket out of in_progress, route it to a terminal state based on why
    the loop stopped.

    Agents *should* call set_status themselves. This ensures no ticket is
    ever left orphaned by a tick that finished normally.
    """
    fresh = tickets.get(ticket.id)
    if fresh is None or fresh.status != "in_progress":
        return
    reason = summary.get("stop_reason", "unknown")
    reclaim = int((fresh.metadata or {}).get("reclaim_count", 0))
    has_commented = bool(summary.get("has_commented"))

    # Transient error → retry if budget remains.
    if reason == "llm_error":
        if reclaim >= MAX_RECLAIMS:
            emit(log, "finalize.block",
                 ticket=fresh.identifier, reason=reason, reclaims=reclaim)
            tickets.update_status(
                fresh.id, "blocked", role=role_name,
                metadata_patch={"last_blocked_reason": reason},
            )
        else:
            emit(log, "finalize.retry",
                 ticket=fresh.identifier, reason=reason,
                 reclaims=reclaim + 1)
            tickets.update_status(
                fresh.id, "todo", role=role_name,
                metadata_patch={"reclaim_count": reclaim + 1,
                                "last_stop_reason": reason},
            )
        return

    # Model stopped on its own. If it at least posted a comment, treat as
    # handoff; otherwise block for human review.
    if reason == "model_done":
        target = "in_review" if has_commented else "blocked"
        patch: dict = {"last_stop_reason": reason, "auto_finalized": True}
        if target == "blocked":
            patch["last_blocked_reason"] = "silent_model_done"
        emit(log, "finalize.auto",
             ticket=fresh.identifier, reason=reason, target=target,
             has_commented=has_commented)

        canonical_role = _canonical_role(role_name)

        # Doer → Feedback instead of straight to in_review.
        # Feedback's verdict_pass will set in_review + queue Learner.
        if target == "in_review" and canonical_role == "doer":
            _route_to_feedback(fresh, role_name, patch, log)
            _write_t1_memory(fresh, role_name, summary, log)
            return

        # Feedback silent-pass fallback: if feedback agent finished model_done
        # and posted a comment but never called verdict_pass/verdict_fail,
        # treat as implicit pass. Small model (edge) often skips tool call.
        if canonical_role == "feedback" and has_commented:
            emit(log, "finalize.feedback_implicit_pass",
                 ticket=fresh.identifier)
            # Queue Learner + flip to in_review (mimics verdict_pass).
            from . import tools as tools_mod
            try:
                from .tools import _build_learner_digest
                if fresh.parent_id is not None:
                    parent = tickets.get(fresh.parent_id)
                    digest = _build_learner_digest(fresh, parent)
                    tickets.create(
                        title=f"Distil facts: {parent.title[:50] if parent else fresh.identifier}",
                        body=digest,
                        assignee_role="learner",
                        parent_id=fresh.parent_id,
                        priority="low",
                        branch=fresh.branch,
                        project=fresh.project,
                        metadata={"auto_queued_by": "feedback.implicit_pass",
                                  "trigger_ticket": fresh.identifier},
                    )
            except Exception:
                pass
            tickets.update_status(
                fresh.id, "in_review", role=role_name,
                metadata_patch={**patch, "feedback_verdict": "implicit_pass"},
            )
            _write_t1_memory(fresh, role_name, summary, log)
            return

        tickets.update_status(fresh.id, target, role=role_name,
                              metadata_patch=patch)
        if target == "in_review":
            _write_t1_memory(fresh, role_name, summary, log)
        return

    # Wall timeout, max_turns, loop_detected → block for review.
    emit(log, "finalize.block",
         ticket=fresh.identifier, reason=reason, reclaims=reclaim)
    tickets.update_status(
        fresh.id, "blocked", role=role_name,
        metadata_patch={"last_blocked_reason": reason,
                        "last_stop_reason": reason},
    )


# ─────────────────────────── Memory write-back ──────────────────────────
def _write_t1_memory(ticket: tickets.Ticket, role_name: str, summary: dict,
                     log) -> None:
    """Auto-upsert a T1 episodic memory for this ticket.

    Wing = ticket/<identifier>. Text = title + last agent comment + commit
    sha (if any) so `search` + `related_tickets` can surface it later.
    No LLM needed — orchestrator gathers the data from events.
    """
    try:
        from .memory import Memory
        with tickets._conn() as c, c.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT kind, body, metadata FROM ticket_events "
                "WHERE ticket_id=%s ORDER BY created_at DESC LIMIT 40",
                (ticket.id,),
            )
            evts = cur.fetchall()
        last_comment = ""
        commits: list[str] = []
        files_touched: set[str] = set()
        for e in evts:
            if not last_comment and e["kind"] == "comment":
                last_comment = (e["body"] or "")[:1500]
            if e["kind"] == "tool_call":
                meta = e.get("metadata") or {}
                tool = meta.get("tool")
                body = (e.get("body") or "")
                if tool == "git_commit" and "]" not in body[:20]:
                    # crude: first 12 chars of new sha appear in stdout
                    for line in body.splitlines():
                        if len(line) >= 7 and all(
                            c in "0123456789abcdef " for c in line[:7]):
                            commits.append(line.split()[0][:12])
                            break
                if tool in ("edit", "write_file"):
                    args = meta.get("args_preview") or body
                    # extract path=... from the args preview
                    import re
                    m = re.search(r'"path"\s*:\s*"([^"]+)"', args)
                    if m:
                        files_touched.add(m.group(1))

        parts = [
            f"{ticket.identifier}: {ticket.title}",
            f"status={ticket.status}  duration={summary.get('wall_s')}s  "
            f"turns={summary.get('turns')}  by={role_name}",
        ]
        if commits:
            parts.append(f"commits: {', '.join(sorted(set(commits))[:3])}")
        if files_touched:
            fs = sorted(files_touched)[:8]
            parts.append("files: " + ", ".join(fs))
        if last_comment:
            parts.append("summary: " + last_comment)
        text = "\n".join(parts)[:4000]

        mem = Memory()
        rid = mem.retain_fact(
            text=text, tier="t1",
            wing=f"ticket/{ticket.identifier}",
            source=f"orchestrator.finalize@{role_name}",
            metadata={
                "ticket": ticket.identifier,
                "assignee_role": ticket.assignee_role,
                "parent_id": ticket.parent_id,
                "commits": list(sorted(set(commits)))[:3],
                "files": sorted(files_touched)[:8],
                "wing": f"ticket/{ticket.identifier}",
            },
        )
        emit(log, "t1.written", ticket=ticket.identifier, memory_id=rid,
             chars=len(text))
    except Exception as exc:
        emit(log, "t1.write_failed", ticket=ticket.identifier,
             error=str(exc)[:200])


def _canonical_role(name: str) -> str:
    """Map legacy role names (architect/sr_developer/developer/fact_extract)
    onto current canonical names (supervisor/planner/doer/learner)."""
    return {
        "architect": "supervisor",
        "sr_developer": "planner",
        "developer": "doer",
        "fact_extract": "learner",
    }.get(name, name)


def _route_to_feedback(ticket: tickets.Ticket, role_name: str, patch: dict,
                       log) -> None:
    """Doer just set in_review via model_done. Route to Feedback instead."""
    import json as _json
    with tickets._conn() as c, c.cursor() as cur:
        merged = {**patch, "routed_to_feedback_by": role_name,
                  "routed_to_feedback_at": datetime.now(timezone.utc).isoformat()}
        cur.execute(
            "UPDATE tickets SET assignee_role='feedback', status='todo', "
            "metadata = metadata || %s::jsonb WHERE id=%s",
            (_json.dumps(merged), ticket.id),
        )
        c.commit()
    tickets.add_event(
        ticket.id, role_name, "routing",
        body="→ feedback (auto-routed after commit)",
        metadata={"new_assignee": "feedback", "trigger": "doer_complete"},
    )
    emit(log, "doer.route_to_feedback", ticket=ticket.identifier)


def _maybe_queue_fact_extract(ticket: tickets.Ticket, role_name: str,
                              log) -> None:
    """When the Developer lands a child ticket in in_review with a commit,
    auto-queue a fact_extract sibling to distil T3 skills/patterns.

    Safeguards:
      - Only triggers for role_name=='developer' (implementation role).
      - Skips if a fact_extract sibling already exists for this parent.
      - Skips if the ticket itself has no parent (nothing to sibling under).
    """
    if role_name != "developer":
        return
    if ticket.parent_id is None:
        return
    try:
        # Dedup: already a fact_extract ticket under this parent?
        for s in tickets.children(ticket.parent_id):
            if s.assignee_role == "fact_extract" and s.status in (
                "todo", "in_progress", "in_review", "done"
            ):
                return
        parent = tickets.get(ticket.parent_id)
        child = tickets.create(
            title=f"Distil facts: {parent.title[:50] if parent else ticket.identifier}",
            body=(
                f"Post-merge fact distillation for {ticket.identifier} and siblings.\n\n"
                f"Scope: scan recent commits + comments on the parent "
                f"{parent.identifier if parent else ticket.parent_id} and its children. "
                f"Emit up to 5 retain_fact calls (tier='t3', wing='patterns/<topic>' "
                f"or 'skills/<service>'). Anchor each to file:line or commit sha. "
                f"Then post_comment + set_status(done)."
            ),
            assignee_role="fact_extract",
            parent_id=ticket.parent_id,
            priority="low",
            branch=ticket.branch,
            project=ticket.project,
            metadata={"auto_queued_by": "orchestrator.finalize",
                      "trigger_ticket": ticket.identifier},
        )
        emit(log, "fact_extract.queued",
             parent=parent.identifier if parent else None,
             ticket=child.identifier, trigger=ticket.identifier)
    except Exception as exc:
        emit(log, "fact_extract.queue_failed",
             trigger=ticket.identifier, error=str(exc)[:200])


def _finalize_on_exception(ticket: tickets.Ticket, role_name: str,
                           exc: Exception, log) -> None:
    """Exception path: bump reclaim counter, reset to todo (or block if we're
    out of retries)."""
    fresh = tickets.get(ticket.id)
    if fresh is None or fresh.status != "in_progress":
        return
    reclaim = int((fresh.metadata or {}).get("reclaim_count", 0))
    if reclaim >= MAX_RECLAIMS:
        emit(log, "finalize.block",
             ticket=fresh.identifier, reason="exception", reclaims=reclaim)
        tickets.update_status(
            fresh.id, "blocked", role=role_name,
            metadata_patch={"last_blocked_reason": "exception",
                            "last_exception": str(exc)[:500]},
        )
    else:
        emit(log, "finalize.retry",
             ticket=fresh.identifier, reason="exception",
             reclaims=reclaim + 1)
        tickets.update_status(
            fresh.id, "todo", role=role_name,
            metadata_patch={"reclaim_count": reclaim + 1,
                            "last_stop_reason": "exception",
                            "last_exception": str(exc)[:500]},
        )


# ─────────────────────────── Entry ──────────────────────────────────────
def tick(role_name: str) -> int:
    rc = role_cfg_get(role_name)
    log = get_logger(role_name)

    with _role_lock(rc.lock_path) as got:
        if not got:
            emit(log, "lock.skip")
            return 0

        reaped = _reap_orphans(role_name, log)
        if reaped:
            emit(log, "reap.done", role=role_name, count=reaped)

        ticket = tickets.claim_next(role_name)
        if ticket is None:
            emit(log, "tick.idle")
            return 0

        emit(log, "tick.start", ticket=ticket.identifier, title=ticket.title)
        try:
            # Note: claim_next already flipped status to in_progress atomically
            # with SELECT FOR UPDATE SKIP LOCKED, so we don't re-set here.
            # RAM guard: first enforce global (active + wired) ceiling, then
            # ensure this role's model is loaded at the desired ctx.
            if rc.transport == "openai":
                memguard.enforce_ram_ceiling(log, reason=f"pre-{role_name}")
                memguard.ensure_loaded(rc.model, rc.ctx, rc.ttl_s, log)
            worktree = _ensure_branch_and_worktree(ticket)
            emit(log, "worktree.prepared",
                 ticket=ticket.identifier, path=worktree)

            # No target repo — block with a clear instruction instead of
            # running the tool loop against the orchestrator source.
            # Planner / Supervisor tiers are exempt since they work off
            # context bundles + search and don't need a worktree to
            # produce analysis.
            if worktree is None and role_name in ("doer", "feedback"):
                tickets.add_event(
                    ticket.id, role_name, "blocked",
                    body=("no target repo could be identified for this "
                          "ticket. Set the `project` field to one of the "
                          "repos under ~/codeRepo (e.g. PosClientBackend, "
                          "PosServerBackend, MongoDbService, etc.) or "
                          "include the repo name in the ticket title/body."),
                )
                tickets.update_status(
                    ticket.id, "blocked", role=role_name,
                    metadata_patch={"last_blocked_reason": "no_target_repo"},
                )
                emit(log, "tick.end", ticket=ticket.identifier,
                     stop_reason="no_target_repo")
                return 0

            summary = _run_tool_loop(rc, ticket, worktree, log)
            emit(log, "tick.end", ticket=ticket.identifier, **summary)
            _finalize_ticket(ticket, role_name, summary, log)
            # Free tiny-model KV cache immediately instead of waiting for TTL.
            if rc.transport == "openai":
                # Smart rebalance: looks at full queue, keeps only the
                # single non-protected model with most pending work warm,
                # evicts the rest. Stronger than per-tick release.
                memguard.plan_rebalance(log, current_role=role_name)
        except Exception as exc:
            emit(log, "tick.exception", ticket=ticket.identifier,
                 error=str(exc)[:500])
            tickets.add_event(ticket.id, role_name, "error",
                              body=f"orchestrator exception: {exc}")
            _finalize_on_exception(ticket, role_name, exc, log)
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
