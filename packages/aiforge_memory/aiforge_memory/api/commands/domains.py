"""``aiforge-memory domains`` — extract + store semantic domains & flows."""
from __future__ import annotations

import argparse
import json

from aiforge_memory.features.domain import extract as dx
from aiforge_memory.features.domain import store as dstore

from ._driver import driver


def run(args: argparse.Namespace) -> int:
    drv = driver()
    res = dx.extract_domains(drv, repo=args.repo, naming=not args.no_naming)
    counts = dstore.upsert_domains(drv, repo=args.repo,
                                   domains=res.domains, flows=res.flows)
    out = {
        "repo": args.repo,
        "counts": counts,
        "domains": [{"name": d.name, "description": d.description,
                     "services": d.services} for d in res.domains],
        "flows": [{"name": f.name, "description": f.description,
                   "steps": [s["label"] for s in f.steps]} for f in res.flows],
    }
    if args.json:
        print(json.dumps(out, indent=2))
    else:
        print(f"repo={args.repo} domains={counts['domains']} flows={counts['flows']}")
        for d in res.domains:
            print(f"  domain {d.name}: {', '.join(d.services)}")
        for f in res.flows:
            print(f"  flow   {f.name}: {' -> '.join(s['label'] for s in f.steps)}")
    return 0 if (res.domains or res.flows) else (0 if args.allow_empty else 1)


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("domains", help="Extract + store domains & flows for a repo")
    p.add_argument("repo")
    p.add_argument("--json", action="store_true", help="emit full JSON")
    p.add_argument("--no-naming", action="store_true",
                   help="skip the optional LLM naming pass (deterministic labels)")
    p.add_argument("--allow-empty", action="store_true",
                   help="exit 0 even when nothing extracted")
    p.set_defaults(func=run)


__all__ = ["run", "register"]
