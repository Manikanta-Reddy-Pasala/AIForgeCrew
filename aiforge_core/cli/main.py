"""`aiforge ticket ...` CLI — file, list, show, update tickets without a UI.

Subcommands:
    ticket create --title "..." --body "..." --assignee sr_developer [--priority high]
    ticket list [--role sr_developer] [--status todo,in_progress]
    ticket show <ONE-<n>>
    ticket comment <ONE-<n>> --body "..."
    ticket status <ONE-<n>> --status done
    ticket tick <role>                # manual single tick
    ticket trace <ONE-<n>> [--follow] [--lines 200]
                                       # live tail of orchestrator log scoped
                                       # to ticket — same data the UI shows
    ticket llm-trace <ONE-<n>> [--follow] [--limit 10]
                                       # full chat history per agent: messages
                                       # sent to LLM + response text
    ticket logs <role> [--follow] [--lines 200]
                                       # tail of per-role ndjson log
                                       # role: intent|planner|doer|feedback|
                                       #       learner|publish|integration|adk_runner
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from urllib.parse import urlencode
from urllib.request import urlopen

from aiforge_core import tickets as tickets
from aiforge_core.net.ssl import context_for as _ssl_context_for


def _orchestrator_tick(*a, **kw):
    """Lazy import — only `aiforge ticket tick` needs it, and the
    orchestrator module pulls in heavy ADK + LiteLLM deps that we don't
    want to require for trace/logs/show subcommands."""
    from aiforge_core.runtime.orchestrator import tick as _tick  # type: ignore
    return _tick(*a, **kw)


orchestrator_tick = _orchestrator_tick


def _api_base() -> str:
    return os.environ.get("AIFORGE_API_BASE", "http://localhost:8799").rstrip("/")


def _cmd_create(args) -> int:
    t = tickets.create(
        title=args.title, body=args.body or "",
        assignee_role=args.assignee, priority=args.priority,
        project=args.project, labels=args.labels or [],
    )
    print(f"{t.identifier}  id={t.id}  assignee={t.assignee_role}")
    return 0


def _cmd_retrieval_eval(args) -> int:
    """A/B compare cursor-style vs aider-style retrieval (removed — intent module deleted)."""
    print("retrieval-eval subcommand has been removed (legacy intent module deleted).",
          file=sys.stderr)
    return 2


def _cmd_classify(args) -> int:
    """Interactive classifier (removed — intent module deleted)."""
    print("classify subcommand has been removed (legacy intent module deleted).",
          file=sys.stderr)
    return 2


def _cmd_list(args) -> int:
    import psycopg
    from psycopg.rows import dict_row

    from aiforge_core.config.env import AIFORGE_DSN
    q = "SELECT identifier, status, priority, assignee_role, title FROM tickets"
    clauses, params = [], []
    if args.role:
        clauses.append("assignee_role = %s"); params.append(args.role)
    if args.status:
        statuses = [s.strip() for s in args.status.split(",")]
        clauses.append("status = ANY(%s)"); params.append(statuses)
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    q += " ORDER BY created_at DESC LIMIT %s"; params.append(args.limit)
    with psycopg.connect(AIFORGE_DSN, row_factory=dict_row) as c, c.cursor() as cur:
        cur.execute(q, params)
        rows = cur.fetchall()
    for r in rows:
        print(f"{r['identifier']:<8} {r['status']:<12} {r['priority']:<6} "
              f"{(r['assignee_role'] or '-'):<14} {r['title'][:80]}")
    return 0


def _cmd_show(args) -> int:
    t = tickets.get(args.identifier)
    if not t:
        print(f"no such ticket: {args.identifier}", file=sys.stderr); return 2
    print(f"{t.identifier}  {t.status}  {t.priority}  assignee={t.assignee_role}")
    print(f"title: {t.title}")
    print(f"branch: {t.branch or '-'}")
    print(f"parent: {t.parent_id or '-'}")
    print(f"labels: {t.labels}")
    print("--- body ---")
    print(t.body)
    print("--- events (last 30) ---")
    for e in tickets.comments(t.id, limit=30):
        ts = e["created_at"].strftime("%H:%M:%S") if e.get("created_at") else "?"
        print(f"[{ts}] [{e['agent_role'] or '-'}] ({e['kind']}) {(e['body'] or '')[:400]}")
    return 0


def _cmd_comment(args) -> int:
    t = tickets.get(args.identifier)
    if not t:
        print(f"no such ticket: {args.identifier}", file=sys.stderr); return 2
    eid = tickets.add_comment(t.id, "human", args.body)
    print(f"comment event id={eid}")
    return 0


def _cmd_status(args) -> int:
    t = tickets.get(args.identifier)
    if not t:
        print(f"no such ticket: {args.identifier}", file=sys.stderr); return 2
    tickets.update_status(t.id, args.status, role="human")
    print(f"{t.identifier} → {args.status}")
    return 0


def _cmd_tick(args) -> int:
    return orchestrator_tick(args.role)


def main() -> int:
    p = argparse.ArgumentParser(prog="aiforge ticket")
    sub = p.add_subparsers(dest="cmd", required=True)

    ALL_ROLES = ["supervisor", "planner", "doer", "feedback", "learner",
                 # legacy aliases still accepted:
                 "architect", "sr_developer", "developer", "fact_extract"]

    c = sub.add_parser("create")
    c.add_argument("--title", required=True)
    c.add_argument("--body")
    c.add_argument("--assignee", choices=ALL_ROLES, default=None,
                   help="optional — defaults to 'supervisor' for triage")
    c.add_argument("--priority", default="medium",
                   choices=["low", "medium", "high", "urgent"])
    c.add_argument("--project")
    c.add_argument("--labels", nargs="*")
    c.set_defaults(func=_cmd_create)

    l = sub.add_parser("list")
    l.add_argument("--role")
    l.add_argument("--status")
    l.add_argument("--limit", type=int, default=30)
    l.set_defaults(func=_cmd_list)

    s = sub.add_parser("show")
    s.add_argument("identifier")
    s.set_defaults(func=_cmd_show)

    cc = sub.add_parser("comment")
    cc.add_argument("identifier")
    cc.add_argument("--body", required=True)
    cc.set_defaults(func=_cmd_comment)

    st = sub.add_parser("status")
    st.add_argument("identifier")
    st.add_argument("--status", required=True,
                    choices=["todo", "in_progress", "in_review",
                             "done", "blocked", "cancelled"])
    st.set_defaults(func=_cmd_status)

    tk = sub.add_parser("tick")
    tk.add_argument("role", choices=ALL_ROLES)
    tk.set_defaults(func=_cmd_tick)

    re_ = sub.add_parser("retrieval-eval",
                         help="A/B compare cursor-style vs aider-style "
                              "retrieval on the same query")
    re_.add_argument("text", help="natural language query")
    re_.add_argument("--repo", required=True, help="repo name under WORKTREE_ROOT")
    re_.add_argument("--top-k", type=int, default=8, dest="top_k")
    re_.set_defaults(func=_cmd_retrieval_eval)

    cf = sub.add_parser("classify",
                        help="interactive classifier — agent asks "
                             "clarifying questions, persists answers "
                             "to synonyms.yml so they're learned for "
                             "next time")
    cf.add_argument("text", nargs="?", default="",
                    help="natural language ticket text (or pipe via stdin)")
    cf.add_argument("--repo",
                    help="target repo (controls synonyms.yml location). "
                         "Without this, mappings go to the global file.")
    cf.add_argument("--max-rounds", type=int, default=3, dest="max_rounds",
                    help="max clarification rounds before giving up "
                         "(default 3)")
    cf.add_argument("--no-persist", action="store_true",
                    help="don't write learned mappings to synonyms.yml")
    cf.set_defaults(func=_cmd_classify)

    tr = sub.add_parser("trace",
                        help="live SSE tail of the ticket's full agent log")
    tr.add_argument("identifier")
    tr.add_argument("--follow", action="store_true",
                    help="stream forever (default: print initial backlog and exit)")
    tr.add_argument("--lines", type=int, default=200,
                    help="initial backlog size when --follow is unset")
    tr.set_defaults(func=_cmd_trace)

    lt = sub.add_parser("llm-trace",
                        help="per-agent LLM chat history for a ticket "
                             "(messages sent + response, dur_ms)")
    lt.add_argument("identifier")
    lt.add_argument("--follow", action="store_true")
    lt.add_argument("--limit", type=int, default=10,
                    help="non-stream: last N llm.call events")
    lt.add_argument("--full", action="store_true",
                    help="print every message body in full (default: heads only)")
    lt.set_defaults(func=_cmd_llm_trace)

    lg = sub.add_parser("logs", help="live tail per-role ndjson log")
    lg.add_argument("role",
                    choices=["intent", "planner", "doer", "feedback",
                             "learner", "publish", "integration",
                             "adk_runner"])
    lg.add_argument("--follow", action="store_true")
    lg.add_argument("--lines", type=int, default=200)
    lg.set_defaults(func=_cmd_role_logs)

    args = p.parse_args()
    return args.func(args)


def _cmd_trace(args) -> int:
    url = f"{_api_base()}/api/trace/{args.identifier}/stream"
    return _stream_sse(url, args.follow, args.lines)


def _cmd_llm_trace(args) -> int:
    if args.follow:
        url = f"{_api_base()}/api/llm-trace/{args.identifier}/stream"
        return _stream_sse(url, True, 0, render=_render_llm_call,
                           full=args.full)
    qs = urlencode({"limit": args.limit})
    url = f"{_api_base()}/api/llm-trace/{args.identifier}?{qs}"
    try:
        with urlopen(url, timeout=10, context=_ssl_context_for(url)) as r:
            data = json.loads(r.read())
    except Exception as exc:
        print(f"error fetching {url}: {exc}", file=sys.stderr); return 2
    if data.get("error"):
        print(f"server error: {data['error']}", file=sys.stderr); return 2
    events = data.get("events") or []
    if not events:
        print(f"(no llm.call events for {args.identifier})")
        return 0
    print(f"=== {len(events)} llm.call events for {args.identifier} ===")
    for i, ev in enumerate(events, 1):
        print(_render_llm_call(ev, full=args.full, prefix=f"#{i}"))
    return 0


def _cmd_role_logs(args) -> int:
    url = f"{_api_base()}/api/logs/{args.role}/stream"
    return _stream_sse(url, args.follow, args.lines)


def _stream_sse(url: str, follow: bool, lines_cap: int,
                *, render=None, **render_kw) -> int:
    """Tail an SSE endpoint. When follow=False, print the first lines_cap
    events then exit; when follow=True, stream until ctrl-c."""
    try:
        r = urlopen(url, timeout=300, context=_ssl_context_for(url))
    except Exception as exc:
        print(f"error connecting {url}: {exc}", file=sys.stderr); return 2
    n = 0
    try:
        while True:
            line = r.readline()
            if not line:
                if follow:
                    time.sleep(0.5); continue
                break
            line = line.decode("utf-8", "replace").rstrip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload:
                continue
            try:
                obj = json.loads(payload)
            except Exception:
                obj = {"line": payload}
            if render:
                print(render(obj, **render_kw))
            else:
                # default: print the inner 'line' or whole payload
                print(obj.get("line") or json.dumps(obj))
            n += 1
            if not follow and n >= lines_cap:
                break
    except KeyboardInterrupt:
        pass
    return 0


def _render_llm_call(obj: dict, *, full: bool = False,
                     prefix: str = "") -> str:
    """Compact one-call summary. Honours --full to dump message bodies."""
    role = obj.get("agent_role") or "?"
    dur = obj.get("dur_ms")
    err = obj.get("error")
    msgs = obj.get("messages") or []
    resp = obj.get("response") or ""
    head = (
        f"{prefix} agent={role} dur_ms={dur} "
        f"msgs={len(msgs)} resp_chars={len(resp)}"
    )
    if err:
        head += f" ERR={err[:160]}"
    if not full:
        return head
    parts = [head]
    for m in msgs:
        r = m.get("role") or "?"
        c = m.get("content") or ""
        parts.append(f"  → {r}: {c[:1200]}")
    if resp:
        parts.append(f"  ← assistant: {resp[:2000]}")
    return "\n".join(parts)


if __name__ == "__main__":
    sys.exit(main())
