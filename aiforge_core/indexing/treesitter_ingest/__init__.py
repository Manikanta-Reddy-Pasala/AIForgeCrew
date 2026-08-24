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

This module was split (grouped by concern) into ``_models`` / ``_setup`` /
``_java_parser`` / ``_tags_helpers`` / ``_neo4j`` submodules; this package
re-exports the full former top-level surface so
``from aiforge_core.indexing import treesitter_ingest`` and every
``treesitter_ingest.<name>`` attribute access is unchanged. The
monkeypatch-coupled generic-tag glue (``_import_aider`` / ``_get_repomap`` /
``_parse_via_tags``) and the ``ingest_repo`` entry point stay defined HERE so
tests that ``monkeypatch.setattr(tsi, ...)`` continue to observe the patch.
"""
from __future__ import annotations

import time
from pathlib import Path

from aiforge_core.observability.logging import emit, get_logger

from ._models import FileParseResult, FileRecord, IngestStats, SymbolRecord
from ._setup import (
    DEFAULT_EXCLUDE_DIRS,
    HARD_FILE_BYTE_LIMIT,
    JAVA_LANG,
    JAVA_PARSER,
    LARGE_FILE_LINE_THRESHOLD,
    LOG_EVERY_N_FILES,
    Language,
    Parser,
    TREESITTER_AVAILABLE,
    tsjava,
)
from ._java_parser import (
    _TYPE_NODE_KINDS,
    _extract_type,
    _find_all,
    _find_child,
    _modifier_strings,
    _node_text,
    _parse_java_file,
)
from ._tags_helpers import (
    _CLASS_NODE_HINTS,
    _FUNC_NODE_HINTS,
    _JS_IMPORT_RE,
    _PY_IMPORT_RE,
    _classify_def,
    _module_for,
    _scan_imports,
    _tag_parser,
)
from ._neo4j import (
    _ALL_SOURCE_SUFFIXES,
    _CALLS_MERGE,
    _DEFINES_MERGE,
    _EXTENDS_MERGE,
    _FILE_CLEAR_DEFINES,
    _FILE_CLEAR_IMPORTS,
    _FILE_EXISTING_SHA1,
    _FILE_MERGE,
    _IMPLEMENTS_MERGE,
    _IMPORTS_MERGE,
    _SYMBOL_MERGE,
    _TAG_SUFFIXES,
    _iter_java_files,
    _iter_source_files,
    _resolve_edges,
    _sha1_bytes,
    _write_file_payload,
)


# ─────────────── generic tag-query extractor (non-java langs) ───────────────
#
# Java keeps its rich hand-written walker (``_java_parser``). Every OTHER
# language is parsed by a GENERIC engine that reuses aider's bundled
# tree-sitter *tags queries* (aider.repomap.RepoMap.get_tags_raw) instead of a
# per-language AST walker — aider ships tags queries for
# python/kotlin/cpp/c/typescript/tsx/… and yields Tag(rel_fname, fname, line,
# name, kind) with kind ∈ {"def","ref"}. We map:
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


def _tag_tree_root(src: bytes, lang: str):
    parser = _tag_parser(lang)
    if parser is None:
        return None
    try:
        return parser.parse(src).root_node
    except Exception:  # noqa: BLE001
        return None


def _def_symbols(tags: list, tree_root, module: str, fpath: Path,
                 repo: str) -> list:
    """One SymbolRecord per definition, deduped by (name, line) — aider emits
    duplicate def tags."""
    out = []
    seen: set[tuple[str, int]] = set()
    for tag in tags:
        if tag.kind != "def":
            continue
        key = (tag.name, tag.line)
        if key in seen:
            continue
        seen.add(key)
        line1 = tag.line + 1 if tag.line >= 0 else 0
        out.append(SymbolRecord(
            fqn=(f"{module}.{tag.name}" if module else tag.name),
            simple=tag.name,
            kind=_classify_def(tree_root, tag.name, tag.line),
            file_path=str(fpath), repo=repo,
            start_line=line1, end_line=line1))
    return out


def _caller_resolver(tags: list, module: str):
    """Caller = nearest def whose (0-based) line is <= the ref line (approximate
    lexical scope); pygments-backfilled refs (line == -1, e.g. cpp/c) and refs
    before any def use a file-level pseudo."""
    def_lines = sorted(((t.line, t.name) for t in tags if t.kind == "def"),
                       key=lambda x: x[0])
    pseudo = f"{module}.<file>" if module else "<file>"

    def _caller_for(ref_line: int) -> str:
        best = None
        if ref_line >= 0:
            for dline, dname in def_lines:
                if dline > ref_line:
                    break
                best = dname
        if not best:
            return pseudo
        return f"{module}.{best}" if module else best
    return _caller_for


def _call_pairs(tags: list, module: str) -> list:
    """``(caller_fqn, callee_simple)`` for each reference, deduped."""
    caller_for = _caller_resolver(tags, module)
    out = []
    seen: set[tuple[str, str]] = set()
    for tag in tags:
        if tag.kind != "ref" or not tag.name:
            continue
        pair = (caller_for(tag.line), tag.name)
        if pair not in seen:
            seen.add(pair)
            out.append(pair)
    return out


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
    module = _module_for(fpath, repo_root)
    result = FileParseResult(file=FileRecord(
        path=str(fpath), repo=repo, sha1=sha1, language=lang or "",
        package=module, loc=(text.count("\n") + 1 if text else 0)))

    rm = _get_repomap()
    if rm is None or not lang:
        return result  # aider unavailable / unknown lang → empty, no crash
    try:
        tags = list(rm.get_tags_raw(str(fpath), fpath.name))
    except Exception:  # noqa: BLE001 — parse/query failure on this file
        return result

    result.symbols = _def_symbols(tags, _tag_tree_root(src, lang), module,
                                 fpath, repo)
    result.call_simples = _call_pairs(tags, module)
    result.imports = _scan_imports(text, lang)
    return result


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


def _walk_suffixes(languages: list[str]) -> tuple:
    """The union of on-disk suffixes the requested languages contribute."""
    suffixes: list[str] = []
    for lg in languages:
        suffixes.extend(_LANG_SUFFIXES.get(lg, ()))
    # `.h` is ambiguous C/C++; include it whenever either is requested.
    if ("cpp" in languages or "c" in languages) and ".h" not in suffixes:
        suffixes.append(".h")
    return tuple(dict.fromkeys(suffixes)) or (".java",)


def _lang_mapper():
    try:
        from grep_ast import filename_to_lang
        return filename_to_lang
    except Exception:  # noqa: BLE001 — grep_ast is a declared dep; degrade
        return None


def _parse_one(fpath: Path, data: bytes, sha1: str, repo_name: str,
               repo_root: Path, to_lang):
    """The right parser for this file, or None when it can't be parsed.

    ``.java`` goes through the rich walker (with its extends/implements/field
    edges); every other supported language is parsed by the generic aider
    tag-query engine, which yields the same shape.
    """
    if fpath.suffix.lower() == ".java":
        if not TREESITTER_AVAILABLE:
            return None            # java grammar missing → skip java files
        return _parse_java_file(fpath, data, repo_name, sha1)
    lang = to_lang(str(fpath)) if to_lang else None
    if not lang:
        return None                # engine can't map this file → skip
    return _parse_via_tags(fpath, data, repo_name, sha1, lang,
                           repo_root=repo_root)


def _ingest_one_file(session, fpath: Path, repo_name: str, repo_root: Path,
                     to_lang, stats: IngestStats):
    """Parse + write one file. Returns its result, or None when skipped."""
    if fpath.stat().st_size > HARD_FILE_BYTE_LIMIT:
        stats.files_skipped_too_big += 1
        return None
    data = fpath.read_bytes()
    sha1 = _sha1_bytes(data)
    rec = session.run(_FILE_EXISTING_SHA1, path=str(fpath),
                      repo=repo_name).single()
    if rec and rec["sha1"] == sha1:
        stats.files_skipped_unchanged += 1
        return None
    parsed = _parse_one(fpath, data, sha1, repo_name, repo_root, to_lang)
    if parsed is None:
        return None
    _write_file_payload(session, parsed, stats)
    stats.files_parsed += 1
    return parsed


def _emit_progress(log, stats: IngestStats, repo_name: str) -> None:
    if stats.files_seen % LOG_EVERY_N_FILES == 0:
        emit(log, "treesitter.progress",
             files_seen=stats.files_seen, files_parsed=stats.files_parsed,
             files_skipped_unchanged=stats.files_skipped_unchanged,
             symbols_written=stats.symbols_written, repo=repo_name)


def _parse_phase(session, repo_root: Path, repo_name: str, suffixes: tuple,
                 to_lang, stats: IngestStats, log) -> list:
    """Phase 1: parse files, write :File + :Symbol + :DEFINES eagerly."""
    parsed_results: list[FileParseResult] = []
    for fpath in _iter_source_files(repo_root, suffixes):
        stats.files_seen += 1
        try:
            parsed = _ingest_one_file(session, fpath, repo_name, repo_root,
                                      to_lang, stats)
            if parsed is not None:
                parsed_results.append(parsed)
        except Exception as exc:  # noqa: BLE001 — one bad file never stops the walk
            stats.files_failed += 1
            log.warning("treesitter.parse_failed",
                        extra={"aiforge": {"file": str(fpath), "err": str(exc)}})
        _emit_progress(log, stats, repo_name)
    return parsed_results


def _edge_phase(session, parsed_results: list, stats: IngestStats, log) -> None:
    """Phase 2: now that ALL :Symbol nodes for this repo exist, resolve
    :CALLS / :EXTENDS / :IMPLEMENTS / :IMPORTS edges by simple-name lookup.
    A second pass means forward-references within the same repo resolve."""
    for parsed in parsed_results:
        try:
            _resolve_edges(session, parsed, stats)
        except Exception as exc:  # noqa: BLE001
            log.warning("treesitter.edge_resolve_failed",
                        extra={"aiforge": {"file": parsed.file.path,
                                           "err": str(exc)}})


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
    suffixes = _walk_suffixes(languages)
    to_lang = _lang_mapper()
    log = get_logger("treesitter_ingest", ticket=None)
    stats = IngestStats(started_at=time.time())
    with driver.session() as session:
        parsed_results = _parse_phase(session, repo_root, repo_name, suffixes,
                                      to_lang, stats, log)
        _edge_phase(session, parsed_results, stats, log)
    stats.finished_at = time.time()
    emit(log, "treesitter.done", repo=repo_name, **stats.as_dict())
    return stats


__all__ = [
    "FileRecord", "SymbolRecord", "FileParseResult", "IngestStats",
    "LARGE_FILE_LINE_THRESHOLD", "HARD_FILE_BYTE_LIMIT", "DEFAULT_EXCLUDE_DIRS",
    "LOG_EVERY_N_FILES", "TREESITTER_AVAILABLE", "JAVA_LANG", "JAVA_PARSER",
    "DEFAULT_LANGUAGES", "ingest_repo",
]
