"""Stage 4 — tree-sitter walk.

For every source file under repo_path:
    - hash the bytes
    - parse with tree-sitter (per-language grammar via tree-sitter-language-pack)
    - run the language-specific query (`queries/<lang>.scm`)
    - emit File_v2 props + Symbol_v2 nodes + DEFINES + IMPORTS edges (lists)

This module returns dataclasses; the writer (`store/symbol_writer.py`)
upserts them into Neo4j. Stage 5 (edges.py) layers CALLS on top.

Languages supported in plan 3:
    .py    -> python
    .java  -> java
    .ts/.tsx -> typescript

Files in unsupported languages are emitted as bare File_v2 nodes
(hash + lang + lines, no symbols).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter import Query, QueryCursor
from tree_sitter_language_pack import get_language, get_parser


# Mapping file extension → tree-sitter language id
_EXT_LANG: dict[str, str] = {
    ".py": "python",
    ".java": "java",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
}

# Skip these directories when walking — don't index build artifacts.
_SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "target", "build", "dist",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".idea", ".vscode", ".DS_Store",
}


@dataclass
class WalkedSymbol:
    fqname: str
    kind: str               # "class" | "interface" | "method" | "function"
    file_path: str
    signature: str = ""
    doc_first_line: str = ""
    line_start: int = 0
    line_end: int = 0


@dataclass
class WalkedFile:
    repo: str
    path: str               # repo-relative
    hash: str
    lang: str
    lines: int
    symbols: list[WalkedSymbol] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    parse_error: bool = False


def lang_for(path: str | Path) -> str | None:
    return _EXT_LANG.get(Path(path).suffix.lower())


def walk_repo(repo_path: str | Path, *, repo: str) -> list[WalkedFile]:
    repo_path = Path(repo_path).resolve()
    out: list[WalkedFile] = []
    for path in _iter_source_files(repo_path):
        rel = str(path.relative_to(repo_path))
        lang = lang_for(rel)
        try:
            data = path.read_bytes()
        except (OSError, ValueError):
            continue
        sha = hashlib.sha256(data).hexdigest()
        lines = data.count(b"\n") + 1
        wf = WalkedFile(repo=repo, path=rel, hash=sha,
                        lang=lang or "other", lines=lines)
        if lang is not None:
            try:
                _parse_into(wf, data, lang)
            except Exception:
                wf.parse_error = True
        out.append(wf)
    return out


def _iter_source_files(root: Path):
    for p in root.rglob("*"):
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        if not p.is_file():
            continue
        if p.suffix.lower() in _EXT_LANG:
            yield p


def _parse_into(wf: WalkedFile, source: bytes, lang: str) -> None:
    parser = get_parser(lang)
    language = get_language(lang)
    tree = parser.parse(source)
    query_text = _load_query(lang)
    if not query_text:
        return
    query = Query(language, query_text)
    cursor = QueryCursor(query)

    classes: list[tuple[str, object]] = []
    methods: list[tuple[str, object]] = []
    functions: list[tuple[str, object]] = []
    imports: list[str] = []

    # `matches()` preserves per-pattern groupings, so name + def of the
    # same match always belong to the same node — robust against the
    # capture-ordering quirks of `captures()`.
    for _match_id, caps in cursor.matches(tree.root_node):
        # caps: dict[capture_name, list[Node]]
        if "class.def" in caps and "class.name" in caps:
            for d, n in zip(caps["class.def"], caps["class.name"]):
                classes.append((_text(n, source), d))
        elif "method.def" in caps and "method.name" in caps:
            for d, n in zip(caps["method.def"], caps["method.name"]):
                methods.append((_text(n, source), d))
        elif "function.def" in caps and "function.name" in caps:
            for d, n in zip(caps["function.def"], caps["function.name"]):
                functions.append((_text(n, source), d))
        elif "import.module" in caps:
            for n in caps["import.module"]:
                imports.append(_text(n, source))
        elif "import.from" in caps:
            for n in caps["import.from"]:
                imports.append(_text(n, source))

    # Build owning-class index for methods (by line ranges)
    class_ranges: list[tuple[int, int, str]] = []
    for cname, cdef in classes:
        class_ranges.append((cdef.start_point[0], cdef.end_point[0], cname))
        wf.symbols.append(_make_symbol(
            wf=wf, name=cname, kind="class", node=cdef, source=source,
        ))
    for mname, mdef in methods:
        owner = _enclosing_class(class_ranges, mdef.start_point[0])
        fqname = _fqname(wf.path, mname, parent_class=owner)
        wf.symbols.append(_make_symbol(
            wf=wf, name=mname, kind="method", node=mdef, source=source,
            fqname=fqname,
        ))
    for fname, fdef in functions:
        # Skip if it's actually a method (already handled)
        if _enclosing_class(class_ranges, fdef.start_point[0]):
            continue
        wf.symbols.append(_make_symbol(
            wf=wf, name=fname, kind="function", node=fdef, source=source,
        ))

    wf.imports = list(dict.fromkeys(imports))   # de-dup, preserve order


def _enclosing_class(
    class_ranges: list[tuple[int, int, str]], line: int,
) -> str | None:
    for start, end, name in class_ranges:
        if start <= line <= end:
            return name
    return None


def _fqname(file_path: str, name: str, *, parent_class: str | None = None) -> str:
    if parent_class:
        return f"{file_path}::{parent_class}::{name}"
    return f"{file_path}::{name}"


def _make_symbol(
    *, wf: WalkedFile, name: str, kind: str, node, source: bytes,
    fqname: str | None = None,
) -> WalkedSymbol:
    sig_line = source.split(b"\n")[node.start_point[0]].decode(
        "utf-8", errors="replace"
    ).strip()
    return WalkedSymbol(
        fqname=fqname or _fqname(wf.path, name),
        kind=kind,
        file_path=wf.path,
        signature=sig_line[:200],
        doc_first_line="",
        line_start=node.start_point[0] + 1,
        line_end=node.end_point[0] + 1,
    )


def _text(node, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _load_query(lang: str) -> str:
    qfile = Path(__file__).parent / "queries" / f"{lang}.scm"
    if not qfile.is_file():
        # tsx maps to typescript query
        if lang == "tsx":
            qfile = Path(__file__).parent / "queries" / "typescript.scm"
        elif lang == "javascript":
            qfile = Path(__file__).parent / "queries" / "typescript.scm"
        if not qfile.is_file():
            return ""
    return qfile.read_text()
