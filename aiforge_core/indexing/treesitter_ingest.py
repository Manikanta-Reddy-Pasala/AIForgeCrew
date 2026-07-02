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


# ─────────────── generic tag-query extractor (non-java langs) ───────────────
#
# Java keeps its rich hand-written walker above. Every OTHER language is parsed
# by a GENERIC engine that reuses aider's bundled tree-sitter *tags queries*
# (aider.repomap.RepoMap.get_tags_raw) instead of a per-language AST walker —
# aider ships tags queries for python/kotlin/cpp/c/typescript/tsx/… and yields
# Tag(rel_fname, fname, line, name, kind) with kind ∈ {"def","ref"}. We map:
#
#   def  → :Symbol   (kind classified by re-querying the node's declaration
#                     ancestor: class-like → "class"; callable → "method";
#                     TitleCase fallback → "class", else "method")
#   ref  → call_simples (callee simple-name; caller = nearest PRECEDING def by
#                        line, else a file-level pseudo-fqn)
#
# extends/implements are NOT reliably recoverable from the generic tags across
# languages, so they stay EMPTY on this path (Java keeps its rich edges). The
# :Symbol nodes + :CALLS edges are the value here.

# Node-type substrings that mark a definition's enclosing declaration. Matched
# against tree-sitter node ``type`` names, which vary per grammar but are
# stable within these families (e.g. ``class_declaration``, ``class_specifier``,
# ``struct_specifier``, ``function_definition``, ``function_declarator``,
# ``method_declaration``, ``object_declaration``).
_CLASS_NODE_HINTS = (
    "class", "struct", "interface", "enum", "trait", "object_declaration",
    "record", "namespace", "type_alias", "module",
)
_FUNC_NODE_HINTS = ("function", "method", "constructor", "lambda", "subroutine")

# aider is a declared dep, but guard the import so a broken/absent wheel
# degrades the multi-language symbol path (returns empty results) instead of
# crashing the whole repo ingest.
_REPOMAP = None            # cached aider RepoMap instance
_REPOMAP_FAILED = False     # sticky: once import/construct fails, stop retrying


def _import_aider():
    """Import aider's RepoMap + InputOutput. Factored out so tests can
    monkeypatch it to simulate aider being unavailable."""
    from aider.io import InputOutput
    from aider.repomap import RepoMap
    return RepoMap, InputOutput


def _get_repomap():
    """Lazy, cached ``aider.repomap.RepoMap``. Returns None (sticky) if aider
    can't be imported/constructed, so the tag path degrades gracefully."""
    global _REPOMAP, _REPOMAP_FAILED
    if _REPOMAP is not None:
        return _REPOMAP
    if _REPOMAP_FAILED:
        return None
    try:
        import tempfile
        RepoMap, InputOutput = _import_aider()
        # RepoMap.get_tags_raw takes an absolute fname and reads the file off
        # disk itself; ``root`` only anchors its (unused-here) tags cache, so a
        # throwaway temp dir keeps the cache out of any real repo.
        root = tempfile.mkdtemp(prefix="aiforge-repomap-")
        _REPOMAP = RepoMap(root=root, io=InputOutput(yes=True))
    except Exception:  # noqa: BLE001 — missing/broken aider or grammars
        _REPOMAP_FAILED = True
        return None
    return _REPOMAP


def _tag_parser(lang: str):
    """tree-sitter parser for ``lang`` via tree-sitter-language-pack (the same
    grammar set aider uses). Returns None on any failure so classification
    falls back to the name-shape heuristic."""
    try:
        from tree_sitter_language_pack import get_parser
        return get_parser(lang)
    except Exception:  # noqa: BLE001
        return None


def _classify_def(root_node, name: str, line: int) -> str:
    """Classify a definition as ``class`` or ``method`` by walking up from the
    name node to its enclosing declaration. Callables are uniformly ``method``
    (not split function/method) so the existing method-only :CALLS writer can
    resolve cross-language calls. Falls back to name shape when the node can't
    be located (TitleCase → class, else method)."""
    if root_node is not None:
        want = name.encode("utf-8", errors="replace")
        # Collect candidate name nodes matching (text, line).
        cands = []
        stack = [root_node]
        while stack:
            n = stack.pop()
            if n.is_named and n.start_point[0] == line and n.text == want:
                cands.append(n)
            stack.extend(n.children)
        for nn in cands:
            p = nn.parent
            depth = 0
            while p is not None and depth < 15:
                t = p.type
                if any(h in t for h in _CLASS_NODE_HINTS):
                    return "class"
                if any(h in t for h in _FUNC_NODE_HINTS):
                    return "method"
                p = p.parent
                depth += 1
    return "class" if name[:1].isupper() else "method"


# Lightweight import scan (secondary signal — imports are only wired as
# :IMPORTS edges when they match a :Symbol fqn, which is rare for external
# libs, so this stays deliberately simple).
_PY_IMPORT_RE = re.compile(r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))",
                           re.MULTILINE)
_JS_IMPORT_RE = re.compile(r"""import\s+.*?from\s+['"]([^'"]+)['"]""")


def _scan_imports(text: str, lang: str) -> list[str]:
    out: list[str] = []
    try:
        if lang == "python":
            for m in _PY_IMPORT_RE.finditer(text):
                mod = m.group(1) or m.group(2)
                if mod:
                    out.append(mod.strip())
        elif lang in ("javascript", "typescript", "tsx", "jsx"):
            out.extend(m.group(1).strip() for m in _JS_IMPORT_RE.finditer(text))
    except Exception:  # noqa: BLE001
        return []
    # de-dup, preserve order
    seen: set[str] = set()
    return [x for x in out if not (x in seen or seen.add(x))]


def _module_for(fpath: Path, repo_root: "Path | None") -> str:
    """Derive the module/package qualifier used in ``fqn`` and ``FileRecord.
    package``. With ``repo_root`` the file's repo-relative path (slashes → dots,
    extension dropped) — a python dotted module for .py, and a unique per-file
    qualifier for others (avoids fqn collisions between same-stem files). Falls
    back to the bare file stem when no root is available (unit tests)."""
    if repo_root is not None:
        try:
            rel = fpath.resolve().relative_to(Path(repo_root).resolve())
        except Exception:  # noqa: BLE001 — not under root
            rel = Path(fpath.name)
        parts = [p for p in rel.with_suffix("").parts if p not in ("", ".")]
        if parts:
            return ".".join(parts)
    return fpath.stem


def _parse_via_tags(
    fpath: Path,
    src: bytes,
    repo: str,
    sha1: str,
    lang: str,
    repo_root: "Path | None" = None,
) -> FileParseResult:
    """Parse ``fpath`` with the generic aider tag-query engine into the same
    ``FileParseResult`` shape as ``_parse_java_file``. Soft-fails to an empty
    result (never raises) so a bad file / missing aider / unsupported lang
    degrades the symbol index instead of crashing the repo ingest."""
    text = src.decode("utf-8", errors="replace")
    loc = text.count("\n") + 1 if text else 0
    module = _module_for(fpath, repo_root)
    file_rec = FileRecord(
        path=str(fpath), repo=repo, sha1=sha1, language=lang or "",
        package=module, loc=loc,
    )
    result = FileParseResult(file=file_rec)

    rm = _get_repomap()
    if rm is None or not lang:
        return result  # aider unavailable / unknown lang → empty, no crash

    try:
        tags = list(rm.get_tags_raw(str(fpath), fpath.name))
    except Exception:  # noqa: BLE001 — parse/query failure on this file
        return result

    tree_root = None
    parser = _tag_parser(lang)
    if parser is not None:
        try:
            tree_root = parser.parse(src).root_node
        except Exception:  # noqa: BLE001
            tree_root = None

    def _fqn(simple: str) -> str:
        return f"{module}.{simple}" if module else simple

    # DEFINITIONS (dedup by (name, line) — aider emits duplicate def tags).
    seen_defs: set[tuple[str, int]] = set()
    for tag in tags:
        if tag.kind != "def":
            continue
        key = (tag.name, tag.line)
        if key in seen_defs:
            continue
        seen_defs.add(key)
        line1 = tag.line + 1 if tag.line >= 0 else 0
        result.symbols.append(SymbolRecord(
            fqn=_fqn(tag.name),
            simple=tag.name,
            kind=_classify_def(tree_root, tag.name, tag.line),
            file_path=str(fpath),
            repo=repo,
            start_line=line1,
            end_line=line1,
        ))

    # REFERENCES → call_simples. Caller = nearest def whose (0-based) line is
    # <= the ref line (approximate lexical scope); pygments-backfilled refs
    # (line == -1, e.g. cpp/c) and refs before any def use a file-level pseudo.
    def_lines = sorted(((tag.line, tag.name) for tag in tags if tag.kind == "def"),
                       key=lambda x: x[0])
    pseudo_caller = f"{module}.<file>" if module else "<file>"

    def _caller_for(ref_line: int) -> str:
        best = None
        if ref_line >= 0:
            for dline, dname in def_lines:
                if dline <= ref_line:
                    best = dname
                else:
                    break
        return _fqn(best) if best else pseudo_caller

    seen_calls: set[tuple[str, str]] = set()
    for tag in tags:
        if tag.kind != "ref":
            continue
        callee = tag.name
        if not callee:
            continue
        pair = (_caller_for(tag.line), callee)
        if pair in seen_calls:
            continue
        seen_calls.add(pair)
        result.call_simples.append(pair)

    result.imports = _scan_imports(text, lang)
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


# ─────────────── public API ───────────────

#: Default language set for a multi-language ingest. ``java`` routes to the
#: rich hand-written walker; every other language goes through the generic
#: aider tag-query engine (``_parse_via_tags``).
DEFAULT_LANGUAGES = [
    "java", "kotlin", "python", "javascript", "typescript", "tsx", "c", "cpp",
]

# Which on-disk suffixes each requested language contributes to the walk.
_LANG_SUFFIXES: dict[str, tuple[str, ...]] = {
    "java": (".java",),
    "kotlin": (".kt", ".kts"),
    "python": (".py", ".pyi"),
    "javascript": (".js", ".jsx", ".mjs", ".cjs"),
    "jsx": (".jsx",),
    "typescript": (".ts",),
    "tsx": (".tsx",),
    "c": (".c", ".h"),
    "cpp": (".cc", ".cpp", ".cxx", ".hpp", ".hxx"),
}


def ingest_repo(
    driver,
    repo_root: Path,
    repo_name: str,
    languages: list[str] | None = None,
) -> IngestStats:
    """Walk ``repo_root``, parse every source file in ``languages``, write to
    Neo4j.

    ``.java`` files go through the rich ``_parse_java_file`` walker (with its
    extends/implements/field edges); every other supported language is parsed
    by the generic aider tag-query engine (``_parse_via_tags``) which yields
    the same ``FileParseResult`` shape, so the Neo4j writers are unchanged.
    Unsupported/unknown languages are skipped (never crash the ingest).
    """
    languages = languages or DEFAULT_LANGUAGES

    # Resolve the union of suffixes to walk from the requested languages.
    suffixes: list[str] = []
    for lg in languages:
        suffixes.extend(_LANG_SUFFIXES.get(lg, ()))
    # `.h` is ambiguous C/C++; include it whenever either is requested.
    if ("cpp" in languages or "c" in languages) and ".h" not in suffixes:
        suffixes.append(".h")
    suffixes_t = tuple(dict.fromkeys(suffixes)) or (".java",)

    try:
        from grep_ast import filename_to_lang
    except Exception:  # noqa: BLE001 — grep_ast is a declared dep; degrade
        filename_to_lang = None  # type: ignore

    log = get_logger("treesitter_ingest", ticket=None)
    stats = IngestStats(started_at=time.time())
    parsed_results: list[FileParseResult] = []

    # Phase 1: parse files, write :File + :Symbol + :DEFINES eagerly.
    with driver.session() as session:
        for fpath in _iter_source_files(repo_root, suffixes_t):
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

                if fpath.suffix.lower() == ".java":
                    if not TREESITTER_AVAILABLE:
                        continue  # java grammar missing → skip java files
                    parsed = _parse_java_file(fpath, data, repo_name, sha1)
                else:
                    lang = filename_to_lang(str(fpath)) if filename_to_lang else None
                    if not lang:
                        continue  # engine can't map this file → skip
                    parsed = _parse_via_tags(
                        fpath, data, repo_name, sha1, lang, repo_root=repo_root)
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
