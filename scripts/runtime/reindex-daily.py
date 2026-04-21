#!/usr/bin/env python3
"""Daily memory reindex (runs ON Mac Studio via launchd at 02:00 local).

Clears + rebuilds the wings most likely to drift:
  code/claude-memory  — operator's personal notes in ~/.claude/memory
  code/project-rules  — CLAUDE.md at repo roots
  code/aiforge        — this repo's own docs + runtime
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

# AIForgeCrew on path
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from aiforge_core.store_v2 import Store
from aiforge_core.rag import reindex_repo


JOBS = [
    ("claude-memory", Path.home() / ".claude",
     ["memory/**/*.md", "CLAUDE.md", "*.md"]),
    ("project-rules", Path.home() / "codeRepo",
     ["CLAUDE.md", "*/CLAUDE.md"]),
    ("aiforge", Path.home() / "AIForgeCrew",
     ["aiforge_core/**/*.py", "scripts/**/*.sh", "scripts/**/*.py",
      "docs/**/*.md", "*.md", "Makefile", "pyproject.toml"]),
]


def main() -> int:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] daily reindex starting")
    store = Store()
    store.ensure_schema()
    total_files = 0
    total_chunks = 0
    for repo, root, sources in JOBS:
        if not root.exists():
            print(f"  - {repo:<15}  {root}  MISSING (skipped)")
            continue
        r = reindex_repo(store, repo=repo, repo_root=root, sources=sources)
        print(f"  + {repo:<15}  {r.files:>3} files  {r.chunks:>4} chunks")
        total_files += r.files
        total_chunks += r.chunks
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
          f"done; {total_files} files → {total_chunks} chunks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
