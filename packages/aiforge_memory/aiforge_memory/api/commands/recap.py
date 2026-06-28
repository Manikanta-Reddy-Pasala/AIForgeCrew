"""``aiforge-memory recap`` — human-readable digest of a repo's recent
memory (gap #5).

Lists the most recent memory nodes for a repo and renders them grouped
by label, so an operator (or a fresh agent session) can eyeball "what do
we already know here?" without opening the graph UI.
"""
from __future__ import annotations

import argparse

from aiforge_memory.features.memory import store as memory_writer

from ._driver import driver


def _render_recap(repo: str, rows: list[dict]) -> str:
    if not rows:
        return f"# Recap — {repo}\n\n(no memory recorded yet)"
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r.get("label") or "Other", []).append(r)
    lines = [f"# Recap — {repo}", ""]
    for label in sorted(groups):
        lines.append(f"## {label} ({len(groups[label])})")
        for r in groups[label]:
            title = r.get("title") or ""
            text = (r.get("text") or "").strip().replace("\n", " ")[:160]
            head = f"- [{r.get('id')}] "
            if title:
                head += f"**{title}** — "
            lines.append(head + text)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def run(args: argparse.Namespace) -> int:
    drv = driver()
    rows = memory_writer.list_memory(drv, repo=args.repo, limit=args.limit)
    print(_render_recap(args.repo, rows))
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("recap", help="Digest of a repo's recent memory")
    p.add_argument("repo")
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(func=run)


__all__ = ["run", "register"]
