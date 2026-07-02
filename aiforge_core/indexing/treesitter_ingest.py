"""Tree-sitter based code-graph ingest for v5 :File / :Symbol layer.

Java-only first pass. Walks a repo, parses each .java file with
``tree_sitter_java``, and emits idempotent Cypher MERGEs into Neo4j:

    (:File {path, repo, sha1, language, package, loc})
    (:Symbol {fqn, simple, kind, file_path, repo, start_line, end_line,
              return_type, param_types, modifiers})

    (File)-[:DEFINES]->(Symbol)
    (Symbol)-[:CALLS]->(Symbol)        — callee resolved by simple name when
                                          fqn is ambiguous
    (File)-[:IMPORTS]->(Symbol)        — for imports we recognise as graph
                                          symbols (i.e. the imported FQN
                                          matches a :Symbol we ingested)
    (Symbol)-[:EXTENDS]->(Symbol)
    (Symbol)-[:IMPLEMENTS]->(Symbol)

Symbol kinds: ``class``, ``interface``, ``enum``, ``record``, ``method``,
``field``. ``fqn`` for a method/field is ``package.Class.member``.

Idempotency: the (File.path, File.repo) composite is the anchor. If
``f.sha1`` matches the on-disk hash, the file is skipped entirely. Symbol
MERGEs use ``fqn`` as the unique key, so re-ingest is safe even after the
sha1 changes.

This pass coexists with the v4 ``scripts/graph_rag/ingest_java.py`` graph
(:Class, :Method, :CALLS) — labels and edges do not collide.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from aiforge_core.observability.logging import emit, get_logger

# tree-sitter + the Java grammar are declared deps (pyproject), but guard the
# import so a broken/absent wheel degrades the symbol index instead of crashing
# module import (and everything that transitively imports it) at startup.
try:
    import tree_sitter_java as tsjava
    from tree_sitter import Language, Parser
    JAVA_LANG = Language(tsjava.language())
    JAVA_PARSER = Parser(JAVA_LANG)
    TREESITTER_AVAILABLE = True
except Exception:  # pragma: no cover — only when the wheel is missing/broken
    tsjava = None  # type: ignore
    Language = Parser = None  # type: ignore
    JAVA_LANG = JAVA_PARSER = None  # type: ignore
    TREESITTER_AVAILABLE = False


# ─────────────── tunables ───────────────

#: Skip method-body symbol extraction (locals, anonymous classes) when a
#: file is bigger than this. Class/method declarations themselves still get
#: ingested. Matches the spec's "handle large files" requirement.
LARGE_FILE_LINE_THRESHOLD = 10_000

#: Files larger than this in bytes are skipped outright (likely generated
#: or vendored). 4 MiB of Java is essentially never hand-written.
HARD_FILE_BYTE_LIMIT = 4 * 1024 * 1024

from aiforge_core.indexing.noise import EXCLUDE_DIRS as DEFAULT_EXCLUDE_DIRS  # shared filter

LOG_EVERY_N_FILES = 50


# ─────────────── data classes ───────────────

@dataclass
class FileRecord:
    path: str
    repo: str
    sha1: str
    language: str
    package: str
    loc: int


@dataclass
class SymbolRecord:
    fqn: str
    simple: str
    kind: str  # class | interface | enum | record | method | field
    file_path: str
    repo: str
    start_line: int
    end_line: int
    return_type: str = ""
    param_types: list[str] = field(default_factory=list)
    modifiers: list[str] = field(default_factory=list)


@dataclass
class FileParseResult:
    file: FileRecord
    symbols: list[SymbolRecord] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    extends_edges: list[tuple[str, str]] = field(default_factory=list)
    implements_edges: list[tuple[str, str]] = field(default_factory=list)
    # caller_fqn -> list of callee simple-names. We resolve simple-name to
    # fqn at ingest time using the global symbol table.
    call_simples: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class IngestStats:
    files_seen: int = 0
    files_parsed: int = 0
    files_skipped_unchanged: int = 0
    files_skipped_too_big: int = 0
    files_failed: int = 0
    symbols_written: int = 0
    calls_written: int = 0
    imports_written: int = 0
    extends_written: int = 0
    implements_written: int = 0
    started_at: float = 0.0
    finished_at: float = 0.0

    def as_dict(self) -> dict:
        return {
            "files_seen": self.files_seen,
            "files_parsed": self.files_parsed,
            "files_skipped_unchanged": self.files_skipped_unchanged,
            "files_skipped_too_big": self.files_skipped_too_big,
            "files_failed": self.files_failed,
            "symbols_written": self.symbols_written,
            "calls_written": self.calls_written,
            "imports_written": self.imports_written,
            "extends_written": self.extends_written,
            "implements_written": self.implements_written,
            "duration_s": round(self.finished_at - self.started_at, 2),
        }


# ─────────────── tree-sitter helpers ───────────────

def _node_text(src: bytes, node) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _find_all(node, kind: str) -> Iterable:
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type == kind:
            yield n
        stack.extend(reversed(n.children))


def _find_child(node, kind: str):
    for c in node.children:
        if c.type == kind:
            return c
    return None


def _modifier_strings(src: bytes, decl) -> list[str]:
    mods = _find_child(decl, "modifiers")
    if mods is None:
        return []
    out: list[str] = []
    for a in mods.children:
        if a.type in ("annotation", "marker_annotation"):
            text = _node_text(src, a).strip()
            out.append(text.split("(", 1)[0])
        elif a.type == "modifier":
            out.append(_node_text(src, a))
        elif a.type in ("public", "private", "protected", "static", "final",
                        "abstract", "synchronized", "native", "default"):
            out.append(a.type)
    return out


# ─────────────── parsing ───────────────

_TYPE_NODE_KINDS = (
    "type_identifier", "generic_type", "void_type", "integral_type",
    "floating_point_type", "boolean_type", "scoped_type_identifier",
    "array_type",
)


def _extract_type(src: bytes, decl) -> str:
    for c in decl.children:
        if c.type in _TYPE_NODE_KINDS:
            return _node_text(src, c).split("\n", 1)[0][:200]
    return ""


def _parse_java_file(path: Path, src: bytes, repo: str, sha1: str) -> FileParseResult:
    if not TREESITTER_AVAILABLE:
        raise RuntimeError(
            "tree-sitter Java grammar unavailable — install `tree-sitter` + "
            "`tree-sitter-java` (they are declared deps; run `uv sync`)")
    tree = JAVA_PARSER.parse(src)
    root = tree.root_node

    pkg = ""
    imports: list[str] = []
    for n in root.children:
        if n.type == "package_declaration":
            pkg = _node_text(src, n).replace("package", "", 1).rstrip(";").strip()
        elif n.type == "import_declaration":
            txt = _node_text(src, n).replace("import", "", 1).rstrip(";").strip()
            txt = txt.replace("static ", "", 1).strip()
            if txt:
                imports.append(txt)

    loc = root.end_point[0] - root.start_point[0] + 1
    file_rec = FileRecord(
        path=str(path),
        repo=repo,
        sha1=sha1,
        language="java",
        package=pkg,
        loc=loc,
    )
    result = FileParseResult(file=file_rec, imports=imports)

    big_file = loc > LARGE_FILE_LINE_THRESHOLD

    type_decl_kinds = (
        ("class_declaration", "class"),
        ("interface_declaration", "interface"),
        ("enum_declaration", "enum"),
        ("record_declaration", "record"),
    )

    for kind_node, kind_label in type_decl_kinds:
        for decl in _find_all(root, kind_node):
            ident = _find_child(decl, "identifier")
            if ident is None:
                continue
            simple = _node_text(src, ident)
            fqn = f"{pkg}.{simple}" if pkg else simple
            cls_sym = SymbolRecord(
                fqn=fqn,
                simple=simple,
                kind=kind_label,
                file_path=str(path),
                repo=repo,
                start_line=decl.start_point[0] + 1,
                end_line=decl.end_point[0] + 1,
                modifiers=_modifier_strings(src, decl),
            )
            result.symbols.append(cls_sym)

            sup = _find_child(decl, "superclass")
            if sup is not None:
                for tc in sup.children:
                    if tc.type in ("type_identifier", "generic_type",
                                   "scoped_type_identifier"):
                        super_simple = _node_text(src, tc).split("<", 1)[0].strip()
                        if super_simple:
                            result.extends_edges.append((fqn, super_simple))

            sup_iface = _find_child(decl, "super_interfaces")
            if sup_iface is not None:
                for sub in _find_all(sup_iface, "type_identifier"):
                    iface_simple = _node_text(src, sub).strip()
                    if iface_simple:
                        result.implements_edges.append((fqn, iface_simple))

            body = (
                _find_child(decl, "class_body")
                or _find_child(decl, "interface_body")
                or _find_child(decl, "enum_body")
            )
            if body is None:
                continue

            for member in body.children:
                if member.type in ("method_declaration", "constructor_declaration"):
                    mident = _find_child(member, "identifier")
                    if mident is None:
                        continue
                    mname = _node_text(src, mident)
                    mfqn = f"{fqn}.{mname}"
                    formal = _find_child(member, "formal_parameters")
                    param_types: list[str] = []
                    if formal is not None:
                        for p in _find_all(formal, "formal_parameter"):
                            t = (_find_child(p, "type_identifier")
                                 or _find_child(p, "generic_type")
                                 or _find_child(p, "scoped_type_identifier")
                                 or _find_child(p, "array_type"))
                            if t is not None:
                                param_types.append(
                                    _node_text(src, t).split("<", 1)[0].strip()
                                )
                    method_sym = SymbolRecord(
                        fqn=mfqn,
                        simple=mname,
                        kind="method",
                        file_path=str(path),
                        repo=repo,
                        start_line=member.start_point[0] + 1,
                        end_line=member.end_point[0] + 1,
                        return_type=_extract_type(src, member),
                        param_types=param_types,
                        modifiers=_modifier_strings(src, member),
                    )
                    result.symbols.append(method_sym)

                    if big_file:
                        continue
                    mbody = _find_child(member, "block")
                    if mbody is None:
                        continue
                    for inv in _find_all(mbody, "method_invocation"):
                        kids = [c for c in inv.children if c.type != "."]
                        idents = [
                            c for c in kids
                            if c.type in ("identifier", "field_access",
                                          "scoped_identifier")
                        ]
                        if not idents:
                            continue
                        callee_simple = _node_text(src, idents[-1]).split(".")[-1]
                        if callee_simple and callee_simple.isidentifier():
                            result.call_simples.append((mfqn, callee_simple))
                elif member.type == "field_declaration":
                    var_decl = _find_child(member, "variable_declarator")
                    if var_decl is None:
                        continue
                    name_node = _find_child(var_decl, "identifier")
                    if name_node is None:
                        continue
                    fname = _node_text(src, name_node)
                    ffqn = f"{fqn}.{fname}"
                    field_sym = SymbolRecord(
                        fqn=ffqn,
                        simple=fname,
                        kind="field",
                        file_path=str(path),
                        repo=repo,
                        start_line=member.start_point[0] + 1,
                        end_line=member.end_point[0] + 1,
                        return_type=_extract_type(src, member),
                        modifiers=_modifier_strings(src, member),
                    )
                    result.symbols.append(field_sym)
    return result


# ─────────────── disk traversal ───────────────

def _sha1_bytes(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def _iter_java_files(repo_root: Path) -> Iterable[Path]:
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [d for d in dirnames if d not in DEFAULT_EXCLUDE_DIRS]
        for fn in filenames:
            if fn.endswith(".java"):
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


# ─────────────── public API ───────────────

def ingest_repo(
    driver,
    repo_root: Path,
    repo_name: str,
    languages: list[str] | None = None,
) -> IngestStats:
    """Walk ``repo_root``, parse every file in ``languages``, write to Neo4j.

    Currently supports ``["java"]`` only. ``languages`` accepted as an
    explicit argument so the signature is stable when Python/TS land.
    """
    languages = languages or ["java"]
    if languages != ["java"]:
        raise NotImplementedError(
            f"treesitter_ingest currently supports java only; got {languages!r}"
        )

    log = get_logger("treesitter_ingest", ticket=None)
    stats = IngestStats(started_at=time.time())
    parsed_results: list[FileParseResult] = []

    # Phase 1: parse files, write :File + :Symbol + :DEFINES eagerly.
    with driver.session() as session:
        for fpath in _iter_java_files(repo_root):
            stats.files_seen += 1
            try:
                size = fpath.stat().st_size
                if size > HARD_FILE_BYTE_LIMIT:
                    stats.files_skipped_too_big += 1
                    continue

                data = fpath.read_bytes()
                sha1 = _sha1_bytes(data)

                rec = session.run(
                    _FILE_EXISTING_SHA1, path=str(fpath), repo=repo_name
                ).single()
                if rec and rec["sha1"] == sha1:
                    stats.files_skipped_unchanged += 1
                    continue

                parsed = _parse_java_file(fpath, data, repo_name, sha1)
                _write_file_payload(session, parsed, stats)
                parsed_results.append(parsed)
                stats.files_parsed += 1
            except Exception as exc:
                stats.files_failed += 1
                log.warning(
                    "treesitter.parse_failed",
                    extra={"aiforge": {"file": str(fpath), "err": str(exc)}},
                )

            if stats.files_seen % LOG_EVERY_N_FILES == 0:
                emit(
                    log, "treesitter.progress",
                    files_seen=stats.files_seen,
                    files_parsed=stats.files_parsed,
                    files_skipped_unchanged=stats.files_skipped_unchanged,
                    symbols_written=stats.symbols_written,
                    repo=repo_name,
                )

        # Phase 2: now that ALL :Symbol nodes for this repo exist, resolve
        # :CALLS / :EXTENDS / :IMPLEMENTS / :IMPORTS edges by simple-name
        # lookup. Doing this in a second pass means forward-references
        # within the same repo resolve correctly.
        for parsed in parsed_results:
            try:
                _resolve_edges(session, parsed, stats)
            except Exception as exc:
                log.warning(
                    "treesitter.edge_resolve_failed",
                    extra={"aiforge": {"file": parsed.file.path, "err": str(exc)}},
                )

    stats.finished_at = time.time()
    emit(log, "treesitter.done", repo=repo_name, **stats.as_dict())
    return stats
