"""codemem Neo4j schema — constraints + indices.

Idempotent: every statement uses IF NOT EXISTS. Safe to re-run.
Plan 1 covers the Repo label only; later plans add Service/File/
Symbol/Chunk in their own apply() steps.

Note: Neo4j 5 rejects a second uniqueness constraint on the same
(label, property) target even when the names differ. Where another
package (e.g. graphify) already owns a uniqueness constraint on
`(:Repo).name`, this module reuses it under whatever name and only
adds its own missing pieces.
"""
from __future__ import annotations


_REPO_NAME_CONSTRAINT_NAME = "codemem_repo_name_unique"

_INDEX_STATEMENTS: list[str] = [
    # B-tree index on last_indexed_at for stats
    "CREATE INDEX codemem_repo_last_indexed_at IF NOT EXISTS "
    "FOR (r:Repo) ON (r.last_indexed_at)",
    # Fulltext over runbook_md so queries like "how do I run X" hit it
    "CREATE FULLTEXT INDEX codemem_repo_runbook_ft IF NOT EXISTS "
    "FOR (r:Repo) ON EACH [r.runbook_md, r.conventions_md]",
    # Service composite uniqueness on (repo, name).
    # NODE KEY is Enterprise-only; IS UNIQUE works on Community.
    "CREATE CONSTRAINT codemem_service_unique IF NOT EXISTS "
    "FOR (s:Service) REQUIRE (s.repo, s.name) IS UNIQUE",
    # B-tree index on (repo, role) for "list services by role" stats
    "CREATE INDEX codemem_service_role IF NOT EXISTS "
    "FOR (s:Service) ON (s.repo, s.role)",
    # File composite uniqueness on (repo, path) — namespaced as File_v2
    # because legacy graphify owns a global :File.path UNIQUE constraint.
    # After Step 10 of the migration plan, the _v2 suffix is dropped.
    "CREATE CONSTRAINT codemem_file_unique IF NOT EXISTS "
    "FOR (f:File_v2) REQUIRE (f.repo, f.path) IS UNIQUE",
    # Symbol composite uniqueness on (repo, fqname). Same _v2 reason as File.
    "CREATE CONSTRAINT codemem_symbol_unique IF NOT EXISTS "
    "FOR (s:Symbol_v2) REQUIRE (s.repo, s.fqname) IS UNIQUE",
    # Symbol kind index — useful for "all classes in repo" queries
    "CREATE INDEX codemem_symbol_kind IF NOT EXISTS "
    "FOR (s:Symbol_v2) ON (s.repo, s.kind)",
    # Fulltext over Symbol signatures so NL query can hit "send method"
    "CREATE FULLTEXT INDEX codemem_symbol_signature_ft IF NOT EXISTS "
    "FOR (s:Symbol_v2) ON EACH [s.signature, s.fqname, s.doc_first_line]",
    # Chunk_v2 — keyed on globally unique id (file_path + offset)
    "CREATE CONSTRAINT codemem_chunk_unique IF NOT EXISTS "
    "FOR (c:Chunk_v2) REQUIRE c.id IS UNIQUE",
    # Vector index for top-K retrieval (bge-m3 1024d cosine)
    "CREATE VECTOR INDEX codemem_chunk_embed IF NOT EXISTS "
    "FOR (c:Chunk_v2) ON c.embed_vec "
    "OPTIONS {indexConfig: {`vector.dimensions`: 1024, "
    "                        `vector.similarity_function`: 'cosine'}}",
]


def _repo_name_constraint_exists(session) -> str | None:
    """Return the name of any uniqueness constraint on (:Repo {name}), or None."""
    rows = list(session.run(
        "SHOW CONSTRAINTS YIELD name, labelsOrTypes, properties, type "
        "WHERE 'Repo' IN labelsOrTypes "
        "  AND properties = ['name'] "
        "  AND type IN ['UNIQUENESS', 'NODE_KEY']"
    ))
    return rows[0]["name"] if rows else None


def apply(driver) -> None:
    """Apply every schema statement. ``driver`` is a neo4j driver.

    Each statement runs in its own session and is idempotent.
    """
    with driver.session() as session:
        existing = _repo_name_constraint_exists(session)
        if existing is None:
            session.run(
                f"CREATE CONSTRAINT {_REPO_NAME_CONSTRAINT_NAME} IF NOT EXISTS "
                "FOR (r:Repo) REQUIRE r.name IS UNIQUE"
            ).consume()

    for stmt in _INDEX_STATEMENTS:
        with driver.session() as session:
            session.run(stmt).consume()
