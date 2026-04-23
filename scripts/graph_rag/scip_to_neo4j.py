#!/usr/bin/env python3
"""Convert SCIP index files (scip-java / scip-typescript / scip-python) into
Neo4j UNWIND batches.

SCIP gives us uniform cross-language symbols, definitions and references.
Domain-specific nodes (annotations, REST paths, Mongo collections, NATS
subjects) are emitted by the language-specific extractors and ingested
via ingest_jsonl.py. Both ingesters MERGE on the same symbol keys so the
graph unifies cleanly.

Usage:
    python scip_to_neo4j.py --scip /path/to/index.scip --repo PosClientBackend \
        --lang java --neo4j bolt://127.0.0.1:7687
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    # scip-python ships Python bindings. scip-java ships proto files we can
    # parse via generic google.protobuf. Fall back to raw descriptor parse.
    from scip_python import scip_pb2  # type: ignore
except Exception:  # pragma: no cover
    scip_pb2 = None  # user must install scip-python or vendor the .proto

from neo4j import GraphDatabase


SCHEMA = [
    "CREATE CONSTRAINT scip_sym IF NOT EXISTS FOR (s:Symbol) REQUIRE s.id IS UNIQUE",
    "CREATE CONSTRAINT scip_doc IF NOT EXISTS FOR (f:File) REQUIRE f.path IS UNIQUE",
    "CREATE CONSTRAINT scip_repo IF NOT EXISTS FOR (r:Repo) REQUIRE r.name IS UNIQUE",
    "CREATE INDEX sym_name IF NOT EXISTS FOR (s:Symbol) ON (s.name)",
    "CREATE INDEX sym_kind IF NOT EXISTS FOR (s:Symbol) ON (s.kind)",
    "CREATE INDEX sym_repo IF NOT EXISTS FOR (s:Symbol) ON (s.repo)",
]

CYPHER_UPSERT_FILES = """
UNWIND $rows AS r
MERGE (f:File {path: r.path})
SET f.repo = r.repo, f.lang = r.lang, f.loc = r.loc
WITH f, r
MATCH (repo:Repo {name: r.repo})
MERGE (repo)-[:HAS_FILE]->(f)
"""

CYPHER_UPSERT_SYMBOLS = """
UNWIND $rows AS r
MERGE (s:Symbol {id: r.id})
SET s.name = r.name, s.kind = r.kind, s.signature = r.signature,
    s.doc = r.doc, s.file = r.file, s.line = r.line,
    s.repo = r.repo, s.lang = r.lang
WITH s, r
MATCH (f:File {path: r.file})
MERGE (f)-[:DEFINES]->(s)
"""

CYPHER_REFS = """
UNWIND $rows AS r
MATCH (from:Symbol {id: r.from})
MATCH (to:Symbol {id: r.to})
MERGE (from)-[rel:REFERENCES]->(to)
ON CREATE SET rel.count = 1
ON MATCH SET rel.count = coalesce(rel.count, 0) + 1
"""

CYPHER_CALLS = """
UNWIND $rows AS r
MATCH (from:Symbol {id: r.from})
MATCH (to:Symbol {id: r.to})
MERGE (from)-[:CALLS {via: 'scip', certainty: 'resolved'}]->(to)
"""


def ensure_schema(session) -> None:
    for stmt in SCHEMA:
        session.run(stmt)


def iter_scip(path: Path):
    if scip_pb2 is None:
        print("ERROR: scip_python not installed; run `pip install scip-python`",
              file=sys.stderr)
        sys.exit(2)
    idx = scip_pb2.Index()
    idx.ParseFromString(path.read_bytes())
    return idx


def _kind_of(sym_info) -> str:
    # SymbolInformation.kind is int enum; map to string.
    kind_map = {
        0: "unknown", 1: "array", 2: "class", 3: "constant", 4: "constructor",
        5: "enum", 6: "enum_member", 7: "event", 8: "field", 9: "file",
        10: "function", 11: "interface", 12: "key", 13: "method",
        14: "module", 15: "namespace", 16: "null", 17: "number", 18: "object",
        19: "operator", 20: "package", 21: "property", 22: "string",
        23: "struct", 24: "type_parameter", 25: "variable",
    }
    return kind_map.get(int(getattr(sym_info, "kind", 0)), "unknown")


def convert(idx, repo: str, lang: str):
    files, symbols, refs, calls = [], [], [], []

    for doc in idx.documents:
        rel = doc.relative_path
        loc = len(doc.text.splitlines()) if doc.text else 0
        files.append({"path": rel, "repo": repo, "lang": lang, "loc": loc})

        # Per-document local symbol infos (definitions).
        for si in doc.symbols:
            symbols.append({
                "id": si.symbol,
                "name": si.display_name or si.symbol.split(".")[-1],
                "kind": _kind_of(si),
                "signature": getattr(si, "signature_documentation", "").text
                             if hasattr(si, "signature_documentation") else "",
                "doc": "\n".join(si.documentation or []),
                "file": rel,
                "line": 0,
                "repo": repo,
                "lang": lang,
            })

        # Occurrences: definitions + references (roles bitflag per SCIP spec).
        for occ in doc.occurrences:
            # role 1 = definition, 8 = import, 16 = write access etc.
            if occ.symbol_roles & 1:
                # Definition line pinpoint; patch the already-pushed symbol.
                start_line = occ.range[0] if occ.range else 0
                for s in symbols:
                    if s["id"] == occ.symbol and s["file"] == rel and s["line"] == 0:
                        s["line"] = start_line + 1
                        break
            else:
                # Reference occurrence — symbol consumed in this doc.
                refs.append({"from": f"file:{rel}", "to": occ.symbol})

    # Call edges: SCIP emits Relationship messages on SymbolInformation;
    # is_reference_by_definition for calls. External indexers differ — treat
    # any `relationship.is_reference` to a method/function as a call.
    for doc in idx.documents:
        for si in doc.symbols:
            for rel in (si.relationships or []):
                if rel.is_reference:
                    calls.append({"from": si.symbol, "to": rel.symbol})

    return files, symbols, refs, calls


def batch(xs, n=500):
    buf = []
    for x in xs:
        buf.append(x)
        if len(buf) >= n:
            yield buf
            buf = []
    if buf:
        yield buf


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scip", required=True, help="Path to index.scip")
    ap.add_argument("--repo", required=True)
    ap.add_argument("--lang", required=True, choices=["java", "ts", "python", "go"])
    ap.add_argument("--neo4j", default="bolt://127.0.0.1:7687")
    ap.add_argument("--user", default="neo4j")
    ap.add_argument("--password", default="password")
    args = ap.parse_args()

    idx = iter_scip(Path(args.scip))
    files, symbols, refs, calls = convert(idx, args.repo, args.lang)
    print(f"scip: files={len(files)} symbols={len(symbols)} "
          f"refs={len(refs)} calls={len(calls)}")

    drv = GraphDatabase.driver(args.neo4j, auth=(args.user, args.password))
    with drv.session() as s:
        ensure_schema(s)
        s.run("MERGE (:Repo {name:$n})", n=args.repo)
        for b in batch(files):
            s.run(CYPHER_UPSERT_FILES, rows=b)
        for b in batch(symbols):
            s.run(CYPHER_UPSERT_SYMBOLS, rows=b)
        for b in batch(calls):
            s.run(CYPHER_CALLS, rows=b)
    drv.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
