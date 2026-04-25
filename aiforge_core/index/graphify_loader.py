"""Load a Graphify ``graph.json`` (NetworkX format) into Neo4j.

Graphify's output schema (v0.4.x, observed empirically on
``MongoDbService/graphify-out/graph.json``)::

    {
      "directed": false,
      "multigraph": false,
      "graph": {},
      "nodes": [
        {
          "id": "users_manikanta_coderepo_<repo>_<rel_path_underscored>",
          "label": "<filename or symbol display name>",
          "file_type": "code" | "rationale",
          "source_file": "/abs/path/to/file.ext",
          "source_location": "L<line>",
          "community": <int>,
          "norm_label": "<lowercased label>"
        },
        ...
      ],
      "links": [
        {
          "source": "<node-id>",
          "target": "<node-id>",
          "relation": "method" | "calls" | "contains" | "rationale_for" | ...,
          "confidence": "EXTRACTED" | "INFERRED",
          "confidence_score": 0.0 .. 1.0,
          "source_file": "/abs/path/to/file.ext",
          "source_location": "L<line>",
          "weight": 1.0
        },
        ...
      ],
      "hyperedges": []
    }

This loader mirrors the graph into Neo4j as ``:File`` / ``:Symbol`` /
``:GraphifyNode`` (catch-all) and the corresponding relationships, tagged
``source: 'graphify'`` so they coexist with the tree-sitter / SCIP ingest
already populated by Phase 3 (``scripts/graph_rag/scip_to_neo4j.py``).

Multi-source tagging: when a node already exists in Neo4j (e.g. from
SCIP ingest), the loader appends ``"graphify"`` to a ``sources`` list
property without dropping the original ``source`` value. Tree-sitter and
Graphify can therefore both annotate the same File/Symbol.

CLI::

    python -m aiforge_core.index.graphify_loader \
        --graph /path/to/graph.json \
        --repo PosClientBackend \
        [--dry-run] [--source-tag graphify]

Returns / prints a stats dict::

    {
      "nodes_created": int, "nodes_skipped": int,
      "edges_created": int, "edges_skipped": int,
      "dur_ms": int, "source_tag": str, "repo": str
    }
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger("aiforge.index.graphify_loader")


# Map Graphify's ``relation`` values to Neo4j relationship type names. All
# relationship types are uppercase per Neo4j convention. Unknown relations
# fall through to a sanitised uppercase form (e.g. "semantically_similar_to"
# -> "SEMANTICALLY_SIMILAR_TO").
RELATION_MAP = {
    "calls": "CALLS",
    "contains": "CONTAINS",
    "method": "DEFINES_METHOD",
    "rationale_for": "RATIONALE_FOR",
    "imports": "IMPORTS",
    "extends": "EXTENDS",
    "implements": "IMPLEMENTS",
    "semantic": "SEMANTICALLY_SIMILAR_TO",
    "semantically_similar_to": "SEMANTICALLY_SIMILAR_TO",
    "extracted": "EXTRACTED",
    "inferred": "INFERRED",
}


SCHEMA_STMTS = [
    # File path is the canonical key used by both tree-sitter (SCIP) and
    # Graphify. Constraint already created by scip_to_neo4j.py; CREATE IF
    # NOT EXISTS makes this idempotent.
    "CREATE CONSTRAINT graphify_file_path IF NOT EXISTS "
    "FOR (f:File) REQUIRE f.path IS UNIQUE",
    # GraphifyNode catch-all keyed by raw graphify node id.
    "CREATE CONSTRAINT graphify_node_id IF NOT EXISTS "
    "FOR (n:GraphifyNode) REQUIRE n.id IS UNIQUE",
    # Symbol may already have an ``id`` constraint from SCIP ingest. Use the
    # same key so MERGE collides with existing rows.
    "CREATE CONSTRAINT graphify_symbol_id IF NOT EXISTS "
    "FOR (s:Symbol) REQUIRE s.id IS UNIQUE",
    "CREATE INDEX graphify_symbol_repo IF NOT EXISTS "
    "FOR (s:Symbol) ON (s.repo)",
    "CREATE INDEX graphify_file_repo IF NOT EXISTS "
    "FOR (f:File) ON (f.repo)",
]


def _classify_node(node: dict) -> str:
    """Return one of ``"file"``, ``"symbol"``, ``"rationale"``, ``"other"``.

    Heuristics (Graphify v0.4.x has no explicit ``kind`` field):

    * ``file_type == "rationale"`` -> rationale
    * ``source_location == "L1"`` AND label looks like a filename
      (contains a dot extension) -> file
    * everything else with ``file_type == "code"`` -> symbol
    * otherwise -> other (catch-all GraphifyNode)
    """
    file_type = node.get("file_type", "")
    label = node.get("label", "") or ""
    location = node.get("source_location", "")

    if file_type == "rationale":
        return "rationale"
    if file_type != "code":
        return "other"
    # Files: graphify emits one node per file at L1, label is the basename
    # with extension (e.g. "MongoDbServiceApplication.java"). Symbols use
    # qualified labels like "MongoDbServiceApplication" or ".main()".
    if location == "L1":
        # File label normally has a dot+extension; parens/leading-dot signal
        # a method/class symbol that just happens to live on line 1.
        if "." in label and "(" not in label and not label.startswith("."):
            return "file"
    return "symbol"


def _relation_type(relation: str | None) -> str:
    if not relation:
        return "RELATED"
    canonical = RELATION_MAP.get(relation.lower())
    if canonical:
        return canonical
    # sanitise: keep alnum + underscore, uppercase
    cleaned = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in relation)
    return cleaned.upper() or "RELATED"


def _line_from_location(location: str | None) -> int | None:
    if not location:
        return None
    raw = str(location).lstrip("Ll")
    # Strip trailing range like "L30-50".
    raw = raw.split("-", 1)[0]
    try:
        return int(raw)
    except ValueError:
        return None


def _ensure_schema(session) -> None:
    for stmt in SCHEMA_STMTS:
        try:
            session.run(stmt)
        except Exception as exc:  # pragma: no cover - depends on Neo4j edition
            log.warning("schema stmt failed (continuing): %s :: %s", stmt, exc)


# Cypher templates ----------------------------------------------------------
# Each MERGE keys on the most stable identifier (path / id) and uses
# ``coalesce`` + list arithmetic to track multi-source tagging. We never
# clobber a pre-existing ``source`` set by tree-sitter ingest.

CYPHER_FILE = """
UNWIND $rows AS r
MERGE (f:File {path: r.path})
ON CREATE SET f.repo = r.repo,
              f.label = r.label,
              f.community = r.community,
              f.source = $source_tag,
              f.sources = [$source_tag],
              f.first_seen_via = $source_tag
ON MATCH SET  f.repo = coalesce(f.repo, r.repo),
              f.label = coalesce(f.label, r.label),
              f.community = coalesce(r.community, f.community),
              f.sources = CASE
                WHEN f.sources IS NULL AND f.source IS NULL THEN [$source_tag]
                WHEN f.sources IS NULL THEN
                     (CASE WHEN f.source = $source_tag THEN [f.source]
                           ELSE [f.source, $source_tag] END)
                WHEN $source_tag IN f.sources THEN f.sources
                ELSE f.sources + $source_tag
              END
SET f.graphify_id = r.graphify_id
"""

CYPHER_SYMBOL = """
UNWIND $rows AS r
MERGE (s:Symbol {id: r.id})
ON CREATE SET s.label = r.label,
              s.name = r.label,
              s.norm_label = r.norm_label,
              s.repo = r.repo,
              s.file = r.source_file,
              s.line = r.line,
              s.community = r.community,
              s.kind = 'graphify-symbol',
              s.source = $source_tag,
              s.sources = [$source_tag],
              s.first_seen_via = $source_tag
ON MATCH SET  s.label = coalesce(s.label, r.label),
              s.norm_label = coalesce(s.norm_label, r.norm_label),
              s.repo = coalesce(s.repo, r.repo),
              s.file = coalesce(s.file, r.source_file),
              s.line = coalesce(s.line, r.line),
              s.community = coalesce(r.community, s.community),
              s.sources = CASE
                WHEN s.sources IS NULL AND s.source IS NULL THEN [$source_tag]
                WHEN s.sources IS NULL THEN
                     (CASE WHEN s.source = $source_tag THEN [s.source]
                           ELSE [s.source, $source_tag] END)
                WHEN $source_tag IN s.sources THEN s.sources
                ELSE s.sources + $source_tag
              END
WITH s, r
WHERE r.source_file IS NOT NULL
MATCH (f:File {path: r.source_file})
MERGE (f)-[d:DEFINES]->(s)
ON CREATE SET d.via = $source_tag
"""

CYPHER_GRAPHIFY_NODE = """
UNWIND $rows AS r
MERGE (n:GraphifyNode {id: r.id})
ON CREATE SET n.label = r.label,
              n.kind = r.kind,
              n.repo = r.repo,
              n.file = r.source_file,
              n.line = r.line,
              n.community = r.community,
              n.source = $source_tag
SET n.norm_label = r.norm_label
"""

# Edge MERGE is dynamic in relationship type, so we use ``apoc.merge.relationship``
# when APOC is present; otherwise we fall back to a per-type batch loop in
# Python. For maximum portability we use the Python loop approach.
CYPHER_EDGE_TYPED = """
UNWIND $rows AS r
MATCH (a) WHERE a.path = r.src OR a.id = r.src
MATCH (b) WHERE b.path = r.tgt OR b.id = r.tgt
MERGE (a)-[rel:%s]->(b)
ON CREATE SET rel.via = $source_tag,
              rel.confidence = r.confidence,
              rel.confidence_score = r.confidence_score,
              rel.weight = r.weight,
              rel.source_file = r.source_file,
              rel.source_location = r.source_location,
              rel.first_seen_via = $source_tag
ON MATCH SET  rel.confidence = coalesce(rel.confidence, r.confidence),
              rel.confidence_score = CASE
                WHEN rel.confidence_score IS NULL THEN r.confidence_score
                ELSE rel.confidence_score
              END
"""


def _row_for_node(node: dict, repo: str) -> tuple[str, dict]:
    """Return ``(category, row)`` where category is one of
    ``file|symbol|rationale|other``.
    """
    cat = _classify_node(node)
    row = {
        "id": node.get("id"),
        "label": node.get("label"),
        "norm_label": node.get("norm_label"),
        "source_file": node.get("source_file"),
        "line": _line_from_location(node.get("source_location")),
        "community": node.get("community"),
        "repo": repo,
    }
    if cat == "file":
        row["path"] = node.get("source_file") or node.get("id")
        row["graphify_id"] = node.get("id")
    if cat == "rationale":
        row["kind"] = "rationale"
    if cat == "other":
        row["kind"] = node.get("file_type", "graphify")
    return cat, row


def _row_for_edge(edge: dict, repo: str) -> dict:
    return {
        "src": edge.get("source"),
        "tgt": edge.get("target"),
        "relation": edge.get("relation"),
        "confidence": edge.get("confidence"),
        "confidence_score": edge.get("confidence_score"),
        "weight": edge.get("weight", 1.0),
        "source_file": edge.get("source_file"),
        "source_location": edge.get("source_location"),
        "repo": repo,
    }


def _batch(rows: Iterable[dict], size: int = 1000):
    buf: list[dict] = []
    for r in rows:
        buf.append(r)
        if len(buf) >= size:
            yield buf
            buf = []
    if buf:
        yield buf


def load_graphify_json(
    driver: Any,
    graph_json_path: Path,
    repo_name: str,
    dry_run: bool = False,
    source_tag: str = "graphify",
    batch_size: int = 1000,
) -> dict:
    """Load a Graphify ``graph.json`` into Neo4j.

    Parameters
    ----------
    driver:
        Active ``neo4j.Driver`` (or compatible). Required when ``dry_run`` is
        False; ignored when ``dry_run`` is True.
    graph_json_path:
        Path to the ``graph.json`` file produced by ``graphify update``.
    repo_name:
        Logical repo name to tag every imported node with (e.g.
        ``"PosClientBackend"`` or ``"_merged"``).
    dry_run:
        If True, the loader parses + classifies rows and returns the same
        stats dict but never writes to Neo4j.
    source_tag:
        Value written to ``source`` / ``sources`` on every node + relationship
        produced by this loader. Defaults to ``"graphify"``.
    batch_size:
        UNWIND chunk size to keep transactions short.

    Returns
    -------
    dict
        ``{nodes_created, nodes_skipped, edges_created, edges_skipped,
           dur_ms, source_tag, repo}``
    """
    t0 = time.time()
    graph_json_path = Path(graph_json_path)
    if not graph_json_path.exists():
        raise FileNotFoundError(graph_json_path)

    with graph_json_path.open() as fh:
        graph = json.load(fh)

    nodes_in: list[dict] = graph.get("nodes", []) or []
    edges_in: list[dict] = graph.get("links", graph.get("edges", [])) or []

    files: list[dict] = []
    symbols: list[dict] = []
    others: list[dict] = []  # rationale + catch-all
    nodes_skipped = 0

    seen_ids: set[str] = set()
    for n in nodes_in:
        nid = n.get("id")
        if not nid or nid in seen_ids:
            nodes_skipped += 1
            continue
        seen_ids.add(nid)
        cat, row = _row_for_node(n, repo_name)
        if cat == "file":
            if not row.get("path"):
                nodes_skipped += 1
                continue
            files.append(row)
        elif cat == "symbol":
            if not row.get("id"):
                nodes_skipped += 1
                continue
            symbols.append(row)
        else:
            row["id"] = nid
            row.setdefault("kind", n.get("file_type", "graphify"))
            others.append(row)

    # Bucket edges by relationship type so we can issue one parameterised
    # Cypher batch per type (Cypher doesn't allow dynamic types in MERGE
    # without APOC).
    edges_by_type: dict[str, list[dict]] = {}
    edges_skipped = 0
    for e in edges_in:
        if not e.get("source") or not e.get("target"):
            edges_skipped += 1
            continue
        rtype = _relation_type(e.get("relation"))
        edges_by_type.setdefault(rtype, []).append(_row_for_edge(e, repo_name))

    stats = {
        "nodes_created": 0,
        "nodes_skipped": nodes_skipped,
        "edges_created": 0,
        "edges_skipped": edges_skipped,
        "dur_ms": 0,
        "source_tag": source_tag,
        "repo": repo_name,
        "files": len(files),
        "symbols": len(symbols),
        "others": len(others),
        "edge_types": {k: len(v) for k, v in edges_by_type.items()},
    }

    if dry_run:
        stats["dur_ms"] = int((time.time() - t0) * 1000)
        log.info("dry-run stats: %s", stats)
        return stats

    if driver is None:
        raise ValueError("driver is required when dry_run=False")

    with driver.session() as session:
        _ensure_schema(session)

        for chunk in _batch(files, batch_size):
            session.run(CYPHER_FILE, rows=chunk, source_tag=source_tag)
            stats["nodes_created"] += len(chunk)

        for chunk in _batch(symbols, batch_size):
            session.run(CYPHER_SYMBOL, rows=chunk, source_tag=source_tag)
            stats["nodes_created"] += len(chunk)

        for chunk in _batch(others, batch_size):
            session.run(CYPHER_GRAPHIFY_NODE, rows=chunk, source_tag=source_tag)
            stats["nodes_created"] += len(chunk)

        for rtype, rows in edges_by_type.items():
            cypher = CYPHER_EDGE_TYPED % rtype
            for chunk in _batch(rows, batch_size):
                session.run(cypher, rows=chunk, source_tag=source_tag)
                stats["edges_created"] += len(chunk)

    stats["dur_ms"] = int((time.time() - t0) * 1000)
    log.info("graphify load complete: %s", stats)
    return stats


# CLI -----------------------------------------------------------------------

def _build_driver_from_env():
    from neo4j import GraphDatabase

    uri = os.environ.get("AIFORGE_NEO4J_URI", "bolt://127.0.0.1:7688")
    user = os.environ.get("AIFORGE_NEO4J_USER", "neo4j")
    password = os.environ.get("AIFORGE_NEO4J_PASSWORD", "password")
    return GraphDatabase.driver(uri, auth=(user, password))


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="aiforge_core.index.graphify_loader",
        description="Load a Graphify graph.json into Neo4j.",
    )
    parser.add_argument("--graph", required=True, help="path to graph.json")
    parser.add_argument(
        "--repo",
        required=True,
        help="logical repo name to tag nodes with (use '_merged' for "
             "the merged multi-repo graph)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="parse + classify only, no Neo4j writes",
    )
    parser.add_argument(
        "--source-tag",
        default="graphify",
        help="value written to node/edge `source` (default: graphify)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=1000,
        help="UNWIND batch size (default: 1000)",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("AIFORGE_LOG_LEVEL", "INFO"),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    driver = None if args.dry_run else _build_driver_from_env()
    try:
        stats = load_graphify_json(
            driver=driver,
            graph_json_path=Path(args.graph),
            repo_name=args.repo,
            dry_run=args.dry_run,
            source_tag=args.source_tag,
            batch_size=args.batch_size,
        )
    finally:
        if driver is not None:
            driver.close()

    json.dump(stats, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
