"""Cypher writer for File_v2, Symbol_v2, DEFINES, IMPORTS, CALLS.

Public surface:
    upsert_files_and_symbols(driver, *, repo, walked_files) -> dict
    upsert_call_edges(driver, *, repo, edges) -> dict

Both idempotent. Stale edges of the same type for files in the
ingest set are pruned before re-inserting.
"""
from __future__ import annotations

from aiforge_core.codemem.ingest.edges import CallEdge
from aiforge_core.codemem.ingest.treesitter_walk import WalkedFile


_UPSERT_FILE = """
MERGE (f:File_v2 {repo: $repo, path: $path})
SET f.hash         = $hash,
    f.lang         = $lang,
    f.lines        = $lines,
    f.parse_error  = $parse_error,
    f.indexed_at   = datetime(),
    f.schema_version = 'codemem-v1'
"""

_UPSERT_SYMBOL = """
MERGE (s:Symbol_v2 {repo: $repo, fqname: $fqname})
SET s.kind            = $kind,
    s.file_path       = $file_path,
    s.signature       = $signature,
    s.doc_first_line  = $doc_first_line,
    s.line_start      = $line_start,
    s.line_end        = $line_end,
    s.schema_version  = 'codemem-v1'
WITH s
MATCH (f:File_v2 {repo: $repo, path: $file_path})
MERGE (f)-[:DEFINES]->(s)
"""

_PRUNE_FILE_SYMBOLS = """
MATCH (f:File_v2 {repo: $repo, path: $path})-[r:DEFINES]->(s:Symbol_v2)
WHERE NOT s.fqname IN $fqnames
DETACH DELETE s
"""

_PRUNE_FILE_IMPORTS = """
MATCH (f:File_v2 {repo: $repo, path: $path})-[r:IMPORTS]->()
DELETE r
"""

_UPSERT_IMPORT_EDGE = """
MATCH (f:File_v2 {repo: $repo, path: $from_path})
MERGE (g:File_v2 {repo: $repo, path: $to_path})
ON CREATE SET g.schema_version = 'codemem-v1'
MERGE (f)-[:IMPORTS]->(g)
"""

_PRUNE_FILE_CALLS = """
MATCH (s:Symbol_v2 {repo: $repo})-[r:CALLS]->()
WHERE s.file_path = $path
DELETE r
"""

_UPSERT_CALL = """
MATCH (a:Symbol_v2 {repo: $repo, fqname: $caller})
MATCH (b:Symbol_v2 {repo: $repo, fqname: $callee})
MERGE (a)-[r:CALLS]->(b)
SET r.confidence = $confidence
"""


def upsert_files_and_symbols(
    driver, *, repo: str, walked_files: list[WalkedFile],
) -> dict:
    counts = {"files": 0, "symbols": 0, "imports": 0, "pruned_symbols": 0}

    file_paths_set = {wf.path for wf in walked_files}

    with driver.session() as sess:
        for wf in walked_files:
            sess.run(
                _UPSERT_FILE,
                repo=repo, path=wf.path, hash=wf.hash,
                lang=wf.lang, lines=wf.lines, parse_error=wf.parse_error,
            ).consume()
            counts["files"] += 1

            # Prune symbols no longer present in this file
            fqnames = [s.fqname for s in wf.symbols]
            r = sess.run(
                _PRUNE_FILE_SYMBOLS,
                repo=repo, path=wf.path, fqnames=fqnames,
            ).consume()
            counts["pruned_symbols"] += r.counters.nodes_deleted

            for sym in wf.symbols:
                sess.run(
                    _UPSERT_SYMBOL,
                    repo=repo, fqname=sym.fqname, kind=sym.kind,
                    file_path=sym.file_path, signature=sym.signature,
                    doc_first_line=sym.doc_first_line,
                    line_start=sym.line_start, line_end=sym.line_end,
                ).consume()
                counts["symbols"] += 1

            # Prune + re-insert imports
            sess.run(_PRUNE_FILE_IMPORTS, repo=repo, path=wf.path).consume()
            for imp in wf.imports:
                # Resolve import to a file path in this repo's walked set
                target = _resolve_import_to_file(imp, file_paths_set)
                if target is None:
                    continue
                sess.run(
                    _UPSERT_IMPORT_EDGE,
                    repo=repo, from_path=wf.path, to_path=target,
                ).consume()
                counts["imports"] += 1

    return counts


def upsert_call_edges(
    driver, *, repo: str, edges: list[CallEdge],
    file_paths: list[str],
) -> dict:
    """Replace CALLS edges sourced from each file in `file_paths`,
    then insert the new edges."""
    counts = {"calls": 0}

    with driver.session() as sess:
        # prune stale CALLS for each file
        for path in file_paths:
            sess.run(_PRUNE_FILE_CALLS, repo=repo, path=path).consume()

        for e in edges:
            sess.run(
                _UPSERT_CALL,
                repo=repo, caller=e.caller_fqname,
                callee=e.callee_fqname, confidence=e.confidence,
            ).consume()
            counts["calls"] += 1

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
