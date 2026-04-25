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

from aiforge_core.legacy.store_v2 import Store
from aiforge_core.legacy.rag import reindex_repo


JOBS = [
    ("claude-memory", Path.home() / ".claude",
     ["memory/**/*.md", "CLAUDE.md", "*.md"]),
    ("project-rules", Path.home() / "codeRepo",
     ["CLAUDE.md", "*/CLAUDE.md"]),
    ("aiforge", Path.home() / "AIForgeCrew",
     ["aiforge_core/**/*.py", "scripts/**/*.sh", "scripts/**/*.py",
      "docs/**/*.md", "*.md", "Makefile", "pyproject.toml"]),
]


def archive_dead_facts(age_days: int = 90) -> int:
    """Move retained T2/T3 facts that have zero hits after `age_days` to
    archived/<original-wing>. Keeps them searchable but deprioritized.
    Returns number archived."""
    import psycopg
    from aiforge_core.runtime.config import AIFORGE_DSN
    with psycopg.connect(AIFORGE_DSN, connect_timeout=5) as c, c.cursor() as cur:
        cur.execute(
            "UPDATE memories SET wing = 'archived/' || wing "
            "WHERE tier IN ('t2', 't3') "
            "AND wing NOT LIKE 'archived/%' "
            "AND COALESCE((metadata->>'hit_count')::int, 0) = 0 "
            "AND (metadata->>'retained_at')::timestamptz < now() - %s::interval "
            "RETURNING id",
            (f'{age_days} days',),
        )
        n = cur.rowcount
        c.commit()
    return n


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
    # Dead-fact archival (B): retained facts with 0 hits after 90d → archived/*
    try:
        n_archived = archive_dead_facts(age_days=90)
        print(f"  + archive_dead_facts: {n_archived} rows moved to archived/*")
    except Exception as exc:
        print(f"  - archive_dead_facts failed: {exc}")
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
          f"done; {total_files} files → {total_chunks} chunks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
