"""Disk traversal + Neo4j write queries and payload writers (split from the
original ``treesitter_ingest`` module — verbatim move, no behaviour change)."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Iterable

from ._models import FileParseResult, IngestStats
from ._setup import DEFAULT_EXCLUDE_DIRS


# ─────────────── disk traversal ───────────────

def _sha1_bytes(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def _iter_java_files(repo_root: Path) -> Iterable[Path]:
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [d for d in dirnames if d not in DEFAULT_EXCLUDE_DIRS]
        for fn in filenames:
            if fn.endswith(".java"):
                yield Path(dirpath) / fn


# Source suffixes handled by the multi-language ingest, mapped to the language
# key we hand the engine (java routes to the rich walker; the rest go through
# _parse_via_tags with grep_ast.filename_to_lang deciding the actual grammar).
_TAG_SUFFIXES = (
    ".kt", ".kts", ".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs",
    ".ts", ".tsx", ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hxx",
)
_ALL_SOURCE_SUFFIXES = (".java",) + _TAG_SUFFIXES


def _iter_source_files(repo_root: Path, suffixes: tuple[str, ...]) -> Iterable[Path]:
    lowered = tuple(s.lower() for s in suffixes)
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [d for d in dirnames if d not in DEFAULT_EXCLUDE_DIRS]
        for fn in filenames:
            if fn.lower().endswith(lowered):
                yield Path(dirpath) / fn


# ─────────────── neo4j writes ───────────────

_FILE_EXISTING_SHA1 = (
    "MATCH (f:File {path: $path, repo: $repo}) RETURN f.sha1 AS sha1"
)

_FILE_MERGE = (
    "MERGE (f:File {path: $path, repo: $repo}) "
    "SET f.sha1 = $sha1, f.language = $language, f.package = $package, "
    "    f.loc = $loc, f.indexed_at = timestamp()"
)

_FILE_CLEAR_DEFINES = (
    # Drop any prior :DEFINES edges from this file so we can rebuild them
    # cleanly. Symbols themselves are kept (other files may reference them).
    "MATCH (f:File {path: $path, repo: $repo})-[r:DEFINES]->() DELETE r"
)

_FILE_CLEAR_IMPORTS = (
    "MATCH (f:File {path: $path, repo: $repo})-[r:IMPORTS]->() DELETE r"
)

_SYMBOL_MERGE = (
    "MERGE (s:Symbol {fqn: $fqn}) "
    "SET s.simple = $simple, s.kind = $kind, s.file_path = $file_path, "
    "    s.repo = $repo, s.start_line = $start_line, s.end_line = $end_line, "
    "    s.return_type = $return_type, s.param_types = $param_types, "
    "    s.modifiers = $modifiers"
)

_DEFINES_MERGE = (
    "MATCH (f:File {path: $path, repo: $repo}), (s:Symbol {fqn: $fqn}) "
    "MERGE (f)-[:DEFINES]->(s)"
)

# Calls: caller fqn -> callee simple name. We resolve to a Symbol by simple
# name within the same repo; if multiple match we pick all of them (low
# certainty). Methods only — fields are skipped here.
_CALLS_MERGE = (
    "MATCH (caller:Symbol {fqn: $caller_fqn}) "
    "MATCH (callee:Symbol {simple: $callee_simple, kind: 'method'}) "
    "WHERE callee.repo = $repo AND callee.fqn <> $caller_fqn "
    "MERGE (caller)-[:CALLS]->(callee) "
    "RETURN count(*) AS n"
)

_EXTENDS_MERGE = (
    "MATCH (child:Symbol {fqn: $child_fqn}) "
    "MATCH (parent:Symbol {simple: $parent_simple}) "
    "WHERE parent.kind IN ['class', 'interface'] AND parent.repo = $repo "
    "MERGE (child)-[:EXTENDS]->(parent) "
    "RETURN count(*) AS n"
)

_IMPLEMENTS_MERGE = (
    "MATCH (cls:Symbol {fqn: $cls_fqn}) "
    "MATCH (iface:Symbol {simple: $iface_simple, kind: 'interface'}) "
    "WHERE iface.repo = $repo "
    "MERGE (cls)-[:IMPLEMENTS]->(iface) "
    "RETURN count(*) AS n"
)

_IMPORTS_MERGE = (
    "MATCH (f:File {path: $path, repo: $repo}) "
    "MATCH (target:Symbol {fqn: $imp_fqn}) "
    "MERGE (f)-[:IMPORTS]->(target) "
    "RETURN count(*) AS n"
)


def _write_file_payload(session, parsed: FileParseResult, stats: IngestStats) -> None:
    f = parsed.file
    session.run(
        _FILE_MERGE,
        path=f.path, repo=f.repo, sha1=f.sha1, language=f.language,
        package=f.package, loc=f.loc,
    )
    session.run(_FILE_CLEAR_DEFINES, path=f.path, repo=f.repo)
    session.run(_FILE_CLEAR_IMPORTS, path=f.path, repo=f.repo)

    for sym in parsed.symbols:
        session.run(
            _SYMBOL_MERGE,
            fqn=sym.fqn, simple=sym.simple, kind=sym.kind,
            file_path=sym.file_path, repo=sym.repo,
            start_line=sym.start_line, end_line=sym.end_line,
            return_type=sym.return_type, param_types=sym.param_types,
            modifiers=sym.modifiers,
        )
        session.run(_DEFINES_MERGE, path=f.path, repo=f.repo, fqn=sym.fqn)
        stats.symbols_written += 1


def _resolve_edges(session, parsed: FileParseResult, stats: IngestStats) -> None:
    repo = parsed.file.repo

    for caller_fqn, callee_simple in parsed.call_simples:
        rec = session.run(
            _CALLS_MERGE,
            caller_fqn=caller_fqn, callee_simple=callee_simple, repo=repo,
        ).single()
        if rec and rec["n"]:
            stats.calls_written += int(rec["n"])

    for child_fqn, parent_simple in parsed.extends_edges:
        rec = session.run(
            _EXTENDS_MERGE,
            child_fqn=child_fqn, parent_simple=parent_simple, repo=repo,
        ).single()
        if rec and rec["n"]:
            stats.extends_written += int(rec["n"])

    for cls_fqn, iface_simple in parsed.implements_edges:
        rec = session.run(
            _IMPLEMENTS_MERGE,
            cls_fqn=cls_fqn, iface_simple=iface_simple, repo=repo,
        ).single()
        if rec and rec["n"]:
            stats.implements_written += int(rec["n"])

    for imp in parsed.imports:
        # Only :Symbol-targeted imports — wildcard imports get the package
        # prefix dropped (they won't match a Symbol fqn directly).
        if imp.endswith(".*"):
            continue
        rec = session.run(
            _IMPORTS_MERGE,
            path=parsed.file.path, repo=repo, imp_fqn=imp,
        ).single()
        if rec and rec["n"]:
            stats.imports_written += int(rec["n"])
