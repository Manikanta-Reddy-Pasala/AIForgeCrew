"""Codebase indexer — AST-chunked upserts into store_v2 T4.

Uses tree-sitter when available for py/ts/js/tsx/java/go. Falls back to
char chunking for other types and for markdown/yaml.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

_MODULE = '<module>'

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

# Generic multi-language globs for external repos.
# Covers Java/Kotlin/Python/TS/JS/Go/MD/YAML. Intentionally ignores
# node_modules, .git, dist, build, target, .venv — excluded via _EXCLUDES.
GENERIC_SOURCES = [
    "**/*.java",
    "**/*.kt",
    "**/*.py",
    "**/*.ts",
    "**/*.tsx",
    "**/*.js",
    "**/*.jsx",
    "**/*.go",
    "**/*.md",
    "**/*.yml",
    "**/*.yaml",
    "**/*.sh",
    "**/*.sql",
]

_EXCLUDES = (
    "node_modules/", ".git/", ".venv/", "venv/",
    "dist/", "build/", "target/", ".next/",
    "__pycache__/", ".pytest_cache/",
    "graphify-out/", ".aiforge/",
)

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


def _chunk_python_regex(text: str) -> list[tuple[str, str]]:
    """Fallback when tree-sitter is unavailable: split on top-level `def` /
    `class` headers and pack into chunk-sized pieces."""
    chunks: list[tuple[str, str]] = []
    buf = ""
    for seg in re.split(r"(?m)^(def |class |async def )", text):
        buf += seg
        if len(buf) >= CHUNK_CHARS:
            chunks.append((_MODULE, buf))
            buf = ""
    if buf:
        chunks.append((_MODULE, buf))
    return chunks or [(_MODULE, text)]


def _walk_definitions(node, name_stack: list, text: str,
                      chunks: list[tuple[str, str]]) -> None:
    """Collect every function/class body, qualified by its enclosing names."""
    if node.type in ("function_definition", "class_definition"):
        name_node = node.child_by_field_name("name")
        name = name_node.text.decode() if name_node else "?"
        chunks.append((".".join(name_stack + [name]),
                       text[node.start_byte:node.end_byte]))
        name_stack = name_stack + [name]
    for child in node.children:
        _walk_definitions(child, name_stack, text, chunks)


def _chunk_python(text: str) -> list[tuple[str, str]]:
    """Return list of (symbol, chunk). Tree-sitter optional; fallback = regex."""
    try:
        import tree_sitter_python as tspy
        from tree_sitter import Language, Parser
    except Exception:  # noqa: BLE001
        return _chunk_python_regex(text)
    parser = Parser(Language(tspy.language()))
    chunks: list[tuple[str, str]] = []
    _walk_definitions(parser.parse(text.encode()).root_node, [], text, chunks)
    return chunks or [(_MODULE, text)]


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


def _indexable(p: Path, repo_root: Path) -> bool:
    """A real file, not in an excluded dir, and under 1 MB — bigger files are
    rarely useful for retrieval."""
    if not p.is_file():
        return False
    rel = p.relative_to(repo_root).as_posix()
    if any(exc in rel + "/" for exc in _EXCLUDES):
        return False
    try:
        return p.stat().st_size <= 1_000_000
    except OSError:
        return False


def _files_to_index(repo_root: Path, sources: list[str]) -> set:
    seen: set[Path] = set()
    for pat in sources:
        for p in repo_root.glob(pat):
            if _indexable(p, repo_root):
                seen.add(p.resolve())
    return seen


def _index_one_file(store: "Store", repo: str, repo_root: Path, f: Path) -> int:
    """Chunk + upsert one file; returns how many chunks landed."""
    try:
        text = f.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    rel = str(f.relative_to(repo_root))
    file_chunks = _chunk_for_path(rel, text)
    for idx, (symbol, chunk) in enumerate(file_chunks):
        # Per-chunk uniqueness of `source`, so upsert-by-source doesn't collapse
        # a multi-chunk file to a single row.
        sym = symbol if len(file_chunks) == 1 else f"{symbol}:{idx}"
        store.upsert_code_chunk(
            repo=repo, path=rel, symbol=sym, text=chunk,
            metadata={"lang": rel.split(".")[-1], "chunk_index": idx})
    return len(file_chunks)


def reindex_repo(store: "Store", *, repo: str, repo_root: Path,
                 sources: list[str] | None = None) -> ReindexResult:
    # Clear existing T4 for this repo
    with store._connect() as c, c.cursor() as cur:
        cur.execute("DELETE FROM memories WHERE tier='t4' AND wing=%s",
                    (f"code/{repo}",))
        c.commit()
    seen = _files_to_index(repo_root, sources or DEFAULT_SOURCES)
    total_chunks = sum(_index_one_file(store, repo, repo_root, f)
                       for f in sorted(seen))
    return ReindexResult(files=len(seen), chunks=total_chunks)
