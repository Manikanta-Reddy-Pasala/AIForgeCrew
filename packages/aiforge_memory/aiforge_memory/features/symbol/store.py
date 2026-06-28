"""Cypher writer for File_v2, Symbol_v2, DEFINES, IMPORTS, CALLS.

Public surface:
    upsert_files_and_symbols(driver, *, repo, walked_files) -> dict
    upsert_call_edges(driver, *, repo, edges) -> dict

Both idempotent. Stale edges of the same type for files in the
ingest set are pruned before re-inserting.

Writes are batched (``UNWIND $rows`` in slices of ``_BATCH``) — one
Cypher round trip per 500 rows instead of one per row.
"""
from __future__ import annotations

from aiforge_memory.features.symbol.extract import WalkedFile
from aiforge_memory.features.symbol.extract_calls import CallEdge

_BATCH = 500

_UPSERT_FILES = """
UNWIND $rows AS r
MERGE (f:File_v2 {repo: $repo, path: r.path})
SET f.hash         = r.hash,
    f.lang         = r.lang,
    f.lines        = r.lines,
    f.parse_error  = r.parse_error,
    f.indexed_at   = datetime(),
    f.schema_version = 'codemem-v1'
"""

_UPSERT_SYMBOLS = """
UNWIND $rows AS r
MERGE (s:Symbol_v2 {repo: $repo, fqname: r.fqname})
SET s.kind            = r.kind,
    s.file_path       = r.file_path,
    s.signature       = r.signature,
    s.doc_first_line  = r.doc_first_line,
    s.line_start      = r.line_start,
    s.line_end        = r.line_end,
    s.visibility      = r.visibility,
    s.modifiers       = r.modifiers,
    s.return_type     = r.return_type,
    s.params_json     = r.params_json,
    s.deprecated      = r.deprecated,
    s.schema_version  = 'codemem-v1'
WITH s, r
MATCH (f:File_v2 {repo: $repo, path: r.file_path})
MERGE (f)-[:DEFINES]->(s)
"""

_PRUNE_FILE_SYMBOLS = """
UNWIND $rows AS r
MATCH (f:File_v2 {repo: $repo, path: r.path})-[:DEFINES]->(s:Symbol_v2)
WHERE NOT s.fqname IN r.fqnames
DETACH DELETE s
"""

_PRUNE_FILE_IMPORTS = """
UNWIND $paths AS p
MATCH (f:File_v2 {repo: $repo, path: p})-[r:IMPORTS]->()
DELETE r
"""

_UPSERT_IMPORT_EDGES = """
UNWIND $rows AS r
MATCH (f:File_v2 {repo: $repo, path: r.from_path})
MERGE (g:File_v2 {repo: $repo, path: r.to_path})
ON CREATE SET g.schema_version = 'codemem-v1'
MERGE (f)-[:IMPORTS]->(g)
"""

_PRUNE_FILE_CALLS = """
UNWIND $paths AS p
MATCH (s:Symbol_v2 {repo: $repo})-[r:CALLS]->()
WHERE s.file_path = p
DELETE r
"""

_UPSERT_CALLS = """
UNWIND $rows AS r
MATCH (a:Symbol_v2 {repo: $repo, fqname: r.caller})
MATCH (b:Symbol_v2 {repo: $repo, fqname: r.callee})
MERGE (a)-[rel:CALLS]->(b)
SET rel.confidence = r.confidence
"""


def _batched(rows: list, n: int = _BATCH):
    for i in range(0, len(rows), n):
        yield rows[i:i + n]


def upsert_files_and_symbols(
    driver, *, repo: str, walked_files: list[WalkedFile],
) -> dict:
    counts = {"files": 0, "symbols": 0, "imports": 0, "pruned_symbols": 0}

    file_paths_set = {wf.path for wf in walked_files}

    file_rows = [
        {"path": wf.path, "hash": wf.hash, "lang": wf.lang,
         "lines": wf.lines, "parse_error": wf.parse_error}
        for wf in walked_files
    ]
    prune_rows = [
        {"path": wf.path, "fqnames": [s.fqname for s in wf.symbols]}
        for wf in walked_files
    ]
    symbol_rows = [
        {"fqname": sym.fqname, "kind": sym.kind,
         "file_path": sym.file_path, "signature": sym.signature,
         "doc_first_line": sym.doc_first_line,
         "line_start": sym.line_start, "line_end": sym.line_end,
         "visibility": getattr(sym, "visibility", "") or "",
         "modifiers": list(getattr(sym, "modifiers", []) or []),
         "return_type": getattr(sym, "return_type", "") or "",
         "params_json": getattr(sym, "params_json", "") or "",
         "deprecated": bool(getattr(sym, "deprecated", False))}
        for wf in walked_files for sym in wf.symbols
    ]
    import_rows = []
    for wf in walked_files:
        for imp in wf.imports:
            # Resolve import to a file path in this repo's walked set
            target = _resolve_import_to_file(imp, file_paths_set)
            if target is None:
                continue
            import_rows.append({"from_path": wf.path, "to_path": target})

    with driver.session() as sess:
        for batch in _batched(file_rows):
            sess.run(_UPSERT_FILES, repo=repo, rows=batch).consume()
            counts["files"] += len(batch)

        # Prune symbols no longer present in each file
        for batch in _batched(prune_rows):
            r = sess.run(_PRUNE_FILE_SYMBOLS, repo=repo, rows=batch).consume()
            counts["pruned_symbols"] += r.counters.nodes_deleted

        for batch in _batched(symbol_rows):
            sess.run(_UPSERT_SYMBOLS, repo=repo, rows=batch).consume()
            counts["symbols"] += len(batch)

        # Prune + re-insert imports
        for batch in _batched([wf.path for wf in walked_files]):
            sess.run(_PRUNE_FILE_IMPORTS, repo=repo, paths=batch).consume()
        for batch in _batched(import_rows):
            sess.run(_UPSERT_IMPORT_EDGES, repo=repo, rows=batch).consume()
            counts["imports"] += len(batch)

    return counts


def upsert_call_edges(
    driver, *, repo: str, edges: list[CallEdge],
    file_paths: list[str],
) -> dict:
    """Replace CALLS edges sourced from each file in `file_paths`,
    then insert the new edges."""
    counts = {"calls": 0}

    edge_rows = [
        {"caller": e.caller_fqname, "callee": e.callee_fqname,
         "confidence": e.confidence}
        for e in edges
    ]

    with driver.session() as sess:
        # prune stale CALLS for each file
        for batch in _batched(list(file_paths)):
            sess.run(_PRUNE_FILE_CALLS, repo=repo, paths=batch).consume()

        for batch in _batched(edge_rows):
            sess.run(_UPSERT_CALLS, repo=repo, rows=batch).consume()
            counts["calls"] += len(batch)

    return counts


_JAVA_PATH_PREFIXES = (
    "src/main/java/",
    "src/test/java/",
    "src/main/kotlin/",
    "src/test/kotlin/",
)


def _resolve_import_to_file(imp: str, file_paths: set[str]) -> str | None:
    """Best-effort: same heuristic as edges._import_candidates.

    Java/Kotlin imports use FQ package names (`com.foo.Bar`) that need
    to be matched against Maven/Gradle paths like
    `src/main/java/com/foo/Bar.java`. We try the bare path first, then
    each well-known src prefix.
    """
    cands: list[str] = []
    if imp.startswith("./") or imp.startswith("../"):
        base = imp.lstrip("./")
        cands.extend([f"{base}.ts", f"{base}.tsx", f"{base}/index.ts"])
    elif "." in imp:
        parts = imp.split(".")
        joined = "/".join(parts)
        # Python style — bare
        cands.append(joined + ".py")
        cands.append(joined + "/__init__.py")
        # Java/Kotlin — bare and Maven-prefixed
        cands.append(joined + ".java")
        cands.append(joined + ".kt")
        for prefix in _JAVA_PATH_PREFIXES:
            cands.append(prefix + joined + ".java")
            cands.append(prefix + joined + ".kt")
    else:
        cands.extend([f"{imp}.py", f"{imp}.java", f"{imp}.ts"])
    for c in cands:
        if c in file_paths:
            return c
    return None
