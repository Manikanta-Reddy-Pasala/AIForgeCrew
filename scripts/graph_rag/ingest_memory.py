#!/usr/bin/env python3
"""Ingest Claude memory markdown files as (:Memory) nodes.

Scans ~/.claude/projects/*/memory/*.md by default, or specific files
passed via --files. Parses frontmatter (name, description, type).

Incremental: reads mtime + content sha; skips unchanged unless --force.

Usage:
    python ingest_memory.py                    # all memories
    python ingest_memory.py --files a.md b.md  # specific
    python ingest_memory.py --force            # re-ingest all
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

import frontmatter
from neo4j import GraphDatabase

MEM_ROOT_DEFAULT = Path(os.path.expanduser("~/.claude/projects"))

SCHEMA = [
    "CREATE CONSTRAINT memory_path IF NOT EXISTS FOR (m:Memory) REQUIRE m.path IS UNIQUE",
    "CREATE INDEX memory_project IF NOT EXISTS FOR (m:Memory) ON (m.project)",
    "CREATE INDEX memory_type IF NOT EXISTS FOR (m:Memory) ON (m.type)",
    "CREATE FULLTEXT INDEX memory_text IF NOT EXISTS FOR (m:Memory) "
    "ON EACH [m.title, m.description, m.body]",
]


def sha16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def collect(files: list[str] | None, root: Path):
    if files:
        for f in files:
            p = Path(f)
            if p.exists() and p.suffix == ".md":
                yield p
        return
    for proj in root.iterdir():
        mdir = proj / "memory"
        if not mdir.is_dir():
            continue
        for md in mdir.rglob("*.md"):
            if md.name == "MEMORY.md":
                continue
            yield md


def project_name(md: Path) -> str:
    # e.g. ~/.claude/projects/-Users-manip-Documents-codeRepo/memory/foo.md
    parts = md.parts
    if "projects" in parts:
        i = parts.index("projects")
        tag = parts[i + 1]
        # strip leading dash, collapse slashes
        tag = tag.lstrip("-").replace("-", "/")
        # last segment is the folder / repo
        return tag.rsplit("/", 1)[-1] or "root"
    return "root"


def parse(md_path: Path) -> dict:
    doc = frontmatter.load(md_path)
    meta = dict(doc.metadata)
    body = doc.content
    return {
        "path": str(md_path),
        "project": project_name(md_path),
        "title": meta.get("name") or md_path.stem,
        "type": meta.get("type") or "note",
        "description": meta.get("description") or "",
        "body": body,
        "mtime": md_path.stat().st_mtime,
        "sha": sha16(body),
    }


UPSERT = """
UNWIND $batch AS m
MERGE (x:Memory {path: m.path})
SET x.project = m.project,
    x.title = m.title,
    x.type = m.type,
    x.description = m.description,
    x.body = m.body,
    x.mtime = m.mtime,
    x.sha = m.sha
"""

SKIP_CHECK = """
UNWIND $batch AS m
OPTIONAL MATCH (x:Memory {path: m.path})
RETURN m.path AS path, x.sha AS existing_sha
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--files", nargs="*")
    ap.add_argument("--root", default=str(MEM_ROOT_DEFAULT))
    ap.add_argument("--neo4j", default="bolt://127.0.0.1:7687")
    ap.add_argument("--user", default="neo4j")
    ap.add_argument("--password", default="password")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    paths = list(collect(args.files, Path(args.root)))
    if not paths:
        print("no memory files found")
        return 0

    records = [parse(p) for p in paths]

    drv = GraphDatabase.driver(args.neo4j, auth=(args.user, args.password))
    with drv.session() as s:
        for stmt in SCHEMA:
            s.run(stmt)

        if not args.force:
            existing = {
                r["path"]: r["existing_sha"]
                for r in s.run(SKIP_CHECK, batch=records)
            }
            records = [r for r in records if existing.get(r["path"]) != r["sha"]]

        if not records:
            print("nothing changed")
            return 0

        for i in range(0, len(records), 100):
            s.run(UPSERT, batch=records[i:i + 100])
    drv.close()
    print(f"ingested {len(records)} memories")
    return 0


if __name__ == "__main__":
    sys.exit(main())
