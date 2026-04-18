"""Hermes CLI — drive a single agent turn against a ticket.

Usage:
    hermes run --role tester --ticket TICKET-xxx --message "..."
    hermes run --role sr-developer --model qwen3.6-35b-a3b --ticket T --message "..."
    hermes tools --role sr-architect    # list tools visible to role
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from paperclip.config import PaperclipConfig
from paperclip.store import Store

from . import __version__
from .agent import Agent
from .llm import LLMClient


def _repo_root() -> Path:
    env = os.environ.get("PAPERCLIP_REPO")
    if env:
        p = Path(env).expanduser().resolve()
        if (p / "paperclip.config.yml").is_file():
            return p
    here = Path.cwd().resolve()
    for c in [here, *here.parents]:
        if (c / "paperclip.config.yml").is_file():
            return c
    raise SystemExit("paperclip.config.yml not found")


def cmd_run(args) -> int:
    repo = _repo_root()
    cfg = PaperclipConfig.load(repo)
    store = Store(repo / ".paperclip" / "paperclip.db") if args.ticket else None
    client: LLMClient
    if args.endpoint:
        client = LLMClient(endpoint=args.endpoint)
    else:
        client = LLMClient.cloud() if args.role == "em" else LLMClient.local()
    agent = Agent.load(repo, args.role, model=args.model, client=client)
    reply = agent.run(args.ticket, args.message, store=store, cfg=cfg if store else None)
    print(f"=== {args.role} reply ===")
    print(reply.content or f"[REASONING]\n{reply.reasoning}")
    print(f"\nusage={reply.usage}")
    if reply.tool_calls:
        print(f"tool_calls={len(reply.tool_calls)}")
    if store:
        store.close()
    return 0


def cmd_tools(args) -> int:
    repo = _repo_root()
    agent = Agent.load(repo, args.role, model="dummy", client=LLMClient(endpoint="http://localhost:1234/v1"))
    for t in agent.registry.list_for_role(args.role):
        print(f"{t.name:<20}  cap={t.capability or '(file ACL)'}  {t.description}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hermes", description="Hermes agent runtime CLI")
    p.add_argument("--version", action="version", version=f"hermes {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run")
    r.add_argument("--role", required=True, choices=["em", "tester", "sr-developer", "sr-architect"])
    r.add_argument("--ticket")
    r.add_argument("--message", required=True)
    r.add_argument("--model")
    r.add_argument("--endpoint")
    r.set_defaults(handler=cmd_run)

    t = sub.add_parser("tools")
    t.add_argument("--role", required=True, choices=["em", "tester", "sr-developer", "sr-architect"])
    t.set_defaults(handler=cmd_tools)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
