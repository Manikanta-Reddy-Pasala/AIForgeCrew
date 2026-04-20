#!/usr/bin/env python3
"""RAG CLI. Invoked via the `rag` bash shim which selects the right venv."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

for candidate in (
    Path("/Users/manikanta/AIForgeCrew"),
    Path("/Users/manip/Documents/codeRepo/AIForgeCrew"),
):
    if (candidate / "aiforge_core").is_dir():
        sys.path.insert(0, str(candidate))
        AIFORGE_ROOT = candidate
        break
else:
    print("aiforge_core not found", file=sys.stderr)
    sys.exit(1)

from aiforge_core.rag import RagIndex


def main() -> int:
    ap = argparse.ArgumentParser(description="Query the aiforge RAG index")
    ap.add_argument("query", help="Natural-language query")
    ap.add_argument("-k", "--top-k", type=int, default=5, help="Number of hits (default 5)")
    ap.add_argument("--repo", default=None, help="Optional repo filter: aiforge|posbackend|tally|mongodbsvc|pds")
    ap.add_argument("--snippet", type=int, default=400, help="Max chars per snippet (default 400)")
    args = ap.parse_args()

    idx = RagIndex(AIFORGE_ROOT)
    hits = idx.query(args.query, top_k=args.top_k * 3 if args.repo else args.top_k)

    if args.repo:
        prefix = f"{args.repo}:"
        if args.repo == "aiforge":
            hits = [h for h in hits if ":" not in h.source]
        else:
            hits = [h for h in hits if h.source.startswith(prefix)]
        hits = hits[: args.top_k]
        if not hits:
            print(f"No hits for repo={args.repo}")
            return 0

    for h in hits:
        snippet = h.text[: args.snippet].replace("\n", " ")
        print(f"[{h.source}]")
        print(f"  {snippet}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
