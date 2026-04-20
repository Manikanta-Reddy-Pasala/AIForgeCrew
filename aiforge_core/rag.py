"""Codebase indexer — AST-chunked upserts into store_v2 T4.

Uses tree-sitter when available for py/ts/js/tsx/java/go. Falls back to
char chunking for other types and for markdown/yaml.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .store_v2 import Store


DEFAULT_SOURCES = [
    "README.md",
    "DESIGN.md",
    "docs/**/*.md",
    "security/**/*.yml",
    "agents/**/*.md",
    "agents/**/*.yml",
    "aiforge_core/**/*.py",
    "scripts/**/*.sh",
    "tools/**/*.py",
]

CHUNK_CHARS = 2500
CHUNK_OVERLAP = 300


_JAVA_METHOD_SIG_RE = re.compile(
    r"^(?: {0,8})(?:@\w+(?:\([^)]*\))?\s*\n(?: {0,8})?)*"
    r"(?:public|private|protected|static|final|synchronized|abstract|\s)+"
    r"[\w<>\[\],\s?]+\s+\w+\s*\([^)]*\)\s*(?:throws [\w, ]+)?\s*\{",
    re.MULTILINE,
)


def _chunk_generic(text: str) -> list[str]:
    if len(text) <= CHUNK_CHARS:
        return [text]
    out: list[str] = []
    i = 0
    while i < len(text):
        out.append(text[i : i + CHUNK_CHARS])
        i += CHUNK_CHARS - CHUNK_OVERLAP
    return out


def _chunk_python(text: str) -> list[tuple[str, str]]:
    """Return list of (symbol, chunk). Tree-sitter optional; fallback = regex."""
    try:
        import tree_sitter_python as tspy
        from tree_sitter import Language, Parser
    except Exception:
        # Fallback: split by top-level `def ` / `class ` headers
        parts = re.split(r"(?m)^(def |class |async def )", text)
        chunks: list[tuple[str, str]] = []
        buf = ""
        for seg in parts:
            buf += seg
            if len(buf) >= CHUNK_CHARS:
                chunks.append(("<module>", buf))
                buf = ""
        if buf:
            chunks.append(("<module>", buf))
        return chunks or [("<module>", text)]

    parser = Parser(Language(tspy.language()))
    tree = parser.parse(text.encode())
    chunks: list[tuple[str, str]] = []

    def walk(node, name_stack):
        if node.type in ("function_definition", "class_definition"):
            name_node = node.child_by_field_name("name")
            name = name_node.text.decode() if name_node else "?"
            qname = ".".join(name_stack + [name])
            start, end = node.start_byte, node.end_byte
            chunks.append((qname, text[start:end]))
            for child in node.children:
                walk(child, name_stack + [name])
        else:
            for child in node.children:
                walk(child, name_stack)

    walk(tree.root_node, [])
    return chunks or [("<module>", text)]


def _chunk_for_path(path: str, text: str) -> list[tuple[str, str]]:
    if path.endswith(".py"):
        return _chunk_python(text)
    if path.endswith(".java"):
        return [("?", c) for c in _chunk_generic(text)]
    return [("<file>", c) for c in _chunk_generic(text)]


@dataclass
class ReindexResult:
    files: int
    chunks: int


def reindex_repo(store: "Store", *, repo: str, repo_root: Path,
                 sources: list[str] | None = None) -> ReindexResult:
    sources = sources or DEFAULT_SOURCES
    # Clear existing T4 for this repo
    with store._connect() as c, c.cursor() as cur:
        cur.execute("DELETE FROM memories WHERE tier='t4' AND wing=%s", (f"code/{repo}",))
        c.commit()

    seen: set[Path] = set()
    for pat in sources:
        for p in repo_root.glob(pat):
            if p.is_file():
                seen.add(p.resolve())

    total_chunks = 0
    for f in sorted(seen):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = str(f.relative_to(repo_root))
        for symbol, chunk in _chunk_for_path(rel, text):
            store.upsert_code_chunk(
                repo=repo, path=rel, symbol=symbol, text=chunk,
                metadata={"lang": rel.split(".")[-1]},
            )
            total_chunks += 1
    return ReindexResult(files=len(seen), chunks=total_chunks)
