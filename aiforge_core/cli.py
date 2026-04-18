"""Paperclip CLI — manage tickets, comments, transitions, audit.

Usage:
    paperclip ticket create --title X --body Y [--assignee engineering_manager]
    paperclip ticket list   [--state S] [--assignee A]
    paperclip ticket show   TICKET-xxx
    paperclip ticket comment TICKET-xxx --author em --body "..."
    paperclip ticket advance TICKET-xxx --to planning --actor em
    paperclip ticket assign  TICKET-xxx --to tester    --actor em
    paperclip audit          TICKET-xxx
    paperclip budget-report  [--role em]
    paperclip doctor          (sanity-check config + agent ACLs vs permission matrix)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

from . import __version__
from .budget import BudgetExceeded, Spend, assert_within_budget, month_usd, record, ticket_tokens
from .config import PaperclipConfig, load_permissions
from .lifecycle import advance as lc_advance
from .lifecycle import allowed_next_states
from .observe import fleet_summary, ticket_report
from .permissions import check as perm_check
from .store import Store


# Resolve repo root: env var first, else walk up from CWD until paperclip.config.yml is found.
def _repo_root() -> Path:
    env = os.environ.get("PAPERCLIP_REPO")
    if env:
        p = Path(env).expanduser().resolve()
        if (p / "paperclip.config.yml").is_file():
            return p
    here = Path.cwd().resolve()
    for candidate in [here, *here.parents]:
        if (candidate / "paperclip.config.yml").is_file():
            return candidate
    raise SystemExit("paperclip.config.yml not found (set PAPERCLIP_REPO or run inside the repo)")


def _store(repo_root: Path) -> Store:
    db = repo_root / ".paperclip" / "paperclip.db"
    return Store(db)


def _fmt_time(t: float) -> str:
    return dt.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S")


# ---- handlers ----
def cmd_ticket_create(args, cfg: PaperclipConfig, store: Store) -> int:
    assignee = args.assignee or cfg.routing.initial_assignee
    t = store.create_ticket(args.title, args.body or "", assignee)
    print(f"{t.id}  assignee={t.assignee}  state={t.state}")
    return 0


def cmd_ticket_list(args, cfg: PaperclipConfig, store: Store) -> int:
    rows = store.list_tickets(state=args.state, assignee=args.assignee)
    if not rows:
        print("(no tickets)")
        return 0
    for t in rows:
        print(f"{t.id}  {t.state:<15}  {t.assignee:<22}  {t.title}")
    return 0


def cmd_ticket_show(args, cfg: PaperclipConfig, store: Store) -> int:
    t = store.get_ticket(args.id)
    if not t:
        print(f"not found: {args.id}", file=sys.stderr); return 1
    print(f"ID:        {t.id}")
    print(f"Title:     {t.title}")
    print(f"State:     {t.state}")
    print(f"Assignee:  {t.assignee}")
    print(f"Created:   {_fmt_time(t.created_at)}")
    print(f"Updated:   {_fmt_time(t.updated_at)}")
    print(f"Body:\n{t.body}")
    print("\nComments:")
    for c in store.list_comments(t.id):
        print(f"  [{_fmt_time(c.created_at)}] {c.author}: {c.body}")
    print(f"\nAllowed next states: {', '.join(allowed_next_states(t.state)) or '(terminal)'}")
    return 0


def cmd_ticket_comment(args, cfg: PaperclipConfig, store: Store) -> int:
    perm_check(cfg.repo_root, args.author, "ticket_comment")
    c = store.add_comment(args.id, args.author, args.body)
    print(f"comment#{c.id} on {c.ticket_id} by {c.author}")
    return 0


def cmd_ticket_advance(args, cfg: PaperclipConfig, store: Store) -> int:
    lc_advance(store, cfg, args.id, args.to, args.actor)
    t = store.get_ticket(args.id)
    print(f"{t.id}  state={t.state}  assignee={t.assignee}")
    return 0


def cmd_ticket_assign(args, cfg: PaperclipConfig, store: Store) -> int:
    perm_check(cfg.repo_root, args.actor, "ticket_assign")
    store.assign(args.id, args.to, args.actor)
    print(f"{args.id} reassigned to {args.to} by {args.actor}")
    return 0


def cmd_audit(args, cfg: PaperclipConfig, store: Store) -> int:
    events = store.list_audit(args.id)
    if not events:
        print("(no audit events)")
        return 0
    for e in events:
        print(f"[{_fmt_time(e['created_at'])}] {e['event']:<10} by {e['actor']:<14}  {json.dumps(e['data'])}")
    return 0


def cmd_budget_report(args, cfg: PaperclipConfig, store: Store) -> int:
    roles = [args.role] if args.role else sorted(cfg.budgets.keys())
    for role in roles:
        b = cfg.budgets.get(role)
        if not b:
            print(f"{role}: (no budget)")
            continue
        usd_m = month_usd(store, role)
        print(f"{role:<16} month_usd={usd_m:7.2f}/{b.cloud_usd_per_month or '∞'}  tokens_cap_per_ticket={b.tokens_per_ticket}")
    return 0


def cmd_report_ticket(args, cfg: PaperclipConfig, store: Store) -> int:
    r = ticket_report(store, args.id)
    if r is None:
        print(f"not found: {args.id}", file=sys.stderr)
        return 1
    print(json.dumps(r.to_dict(), indent=2, default=str))
    return 0


def cmd_report_fleet(args, cfg: PaperclipConfig, store: Store) -> int:
    print(json.dumps(fleet_summary(store, cfg), indent=2, default=str))
    return 0


def cmd_doctor(args, cfg: PaperclipConfig, store: Store) -> int:
    """Sanity checks: config parses, agent ACLs load, DB writable, lifecycle graph sane."""
    print(f"aiforge {__version__}")
    print(f"repo:      {cfg.repo_root}")
    print(f"db:        {(cfg.repo_root / '.paperclip' / 'paperclip.db').resolve()}")
    print(f"audit log: {cfg.audit.log_path}")
    for role in ("em", "tester", "sr-developer", "sr-architect"):
        caps = load_permissions(cfg.repo_root, role)
        true_caps = sum(1 for v in caps.values() if v)
        print(f"  {role:<14} caps_true={true_caps}/{len(caps)}")
    # DB write probe:
    probe = f"PROBE-{int(time.time())}"
    store.audit_event(probe, "doctor", "system", {"ok": True})
    print("  db-write: OK")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="aiforge", description="AIForgeCrew core CLI (Hermes-side orchestrator)")
    p.add_argument("--version", action="version", version=f"aiforge {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("ticket", help="ticket operations")
    tsub = t.add_subparsers(dest="subcmd", required=True)

    pc = tsub.add_parser("create"); pc.add_argument("--title", required=True); pc.add_argument("--body"); pc.add_argument("--assignee")
    pc.set_defaults(handler=cmd_ticket_create)

    pl = tsub.add_parser("list"); pl.add_argument("--state"); pl.add_argument("--assignee")
    pl.set_defaults(handler=cmd_ticket_list)

    ps = tsub.add_parser("show"); ps.add_argument("id")
    ps.set_defaults(handler=cmd_ticket_show)

    pcm = tsub.add_parser("comment"); pcm.add_argument("id"); pcm.add_argument("--author", required=True); pcm.add_argument("--body", required=True)
    pcm.set_defaults(handler=cmd_ticket_comment)

    pa = tsub.add_parser("advance"); pa.add_argument("id"); pa.add_argument("--to", required=True); pa.add_argument("--actor", required=True)
    pa.set_defaults(handler=cmd_ticket_advance)

    pas = tsub.add_parser("assign"); pas.add_argument("id"); pas.add_argument("--to", required=True); pas.add_argument("--actor", required=True)
    pas.set_defaults(handler=cmd_ticket_assign)

    au = sub.add_parser("audit"); au.add_argument("id")
    au.set_defaults(handler=cmd_audit)

    br = sub.add_parser("budget-report"); br.add_argument("--role")
    br.set_defaults(handler=cmd_budget_report)

    rt = sub.add_parser("report-ticket"); rt.add_argument("id")
    rt.set_defaults(handler=cmd_report_ticket)

    rf = sub.add_parser("report-fleet")
    rf.set_defaults(handler=cmd_report_fleet)

    dr = sub.add_parser("doctor")
    dr.set_defaults(handler=cmd_doctor)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = _repo_root()
    cfg = PaperclipConfig.load(repo_root)
    store = _store(repo_root)
    try:
        return args.handler(args, cfg, store)
    except BudgetExceeded as e:
        print(f"BUDGET EXCEEDED: {e}", file=sys.stderr)
        return 3
    except PermissionError as e:
        print(f"PERMISSION DENIED: {e}", file=sys.stderr)
        return 4
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
