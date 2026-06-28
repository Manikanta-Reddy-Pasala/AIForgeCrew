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

import os

_REPO_NAME_CONSTRAINT_NAME = "codemem_repo_name_unique"


# ─── Connection ───────────────────────────────────────────────────────

def neo4j_settings() -> tuple[str, str, str]:
    """Resolve (uri, user, password) from env at call time.

    AIFORGE_NEO4J_* wins, plain NEO4J_* is the fallback, then the local
    dev defaults. Single source of truth — every module that opens a
    driver goes through here (or :func:`open_driver`)."""
    uri = os.environ.get(
        "AIFORGE_NEO4J_URI",
        os.environ.get("NEO4J_URI", "bolt://127.0.0.1:7687"),
    )
    user = os.environ.get(
        "AIFORGE_NEO4J_USER",
        os.environ.get("NEO4J_USER", "neo4j"),
    )
    pw = os.environ.get(
        "AIFORGE_NEO4J_PASSWORD",
        os.environ.get("NEO4J_PASSWORD", "password"),
    )
    return uri, user, pw


def open_driver():
    """Open a Neo4j driver from env config. Errors propagate to caller."""
    from neo4j import GraphDatabase

    uri, user, pw = neo4j_settings()
    return GraphDatabase.driver(uri, auth=(user, pw))


# ─── Vector over-fetch ────────────────────────────────────────────────
#
# Neo4j 5.x has no filtered vector search: db.index.vector.queryNodes()
# ranks the top-K *globally*, and only afterwards does our WHERE clause
# drop other repos' rows. With many repos in one graph a small k can
# return zero same-repo hits. Fix = over-fetch the global stage and keep
# the final LIMIT at the caller's k.

def vector_overfetch_k(k: int, *, cap: int = 500) -> int:
    """Global fetch size for a repo-filtered vector query that should
    yield ~``k`` rows after the repo filter. ``AIFORGE_VECTOR_OVERFETCH``
    (default 10) scales it; capped (default 500) to bound index work."""
    try:
        overfetch = int(os.environ.get("AIFORGE_VECTOR_OVERFETCH", "10"))
    except ValueError:
        overfetch = 10
    return min(max(k, 1) * max(overfetch, 1), cap)

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
    "FOR (s:Symbol_v2) ON EACH [s.signature, s.fqname, s.doc_first_line, s.summary]",
    # Chunk_v2 — keyed on globally unique id (file_path + offset)
    "CREATE CONSTRAINT codemem_chunk_unique IF NOT EXISTS "
    "FOR (c:Chunk_v2) REQUIRE c.id IS UNIQUE",

    # ── Memory layer (Decision_v2 / Observation_v2 / Note_v2) ─────────
    # Decisions: durable architectural / process choices ("we picked X over Y")
    "CREATE CONSTRAINT codemem_decision_unique IF NOT EXISTS "
    "FOR (d:Decision_v2) REQUIRE d.id IS UNIQUE",
    "CREATE INDEX codemem_decision_repo IF NOT EXISTS "
    "FOR (d:Decision_v2) ON (d.repo, d.created_at)",
    "CREATE INDEX codemem_decision_status IF NOT EXISTS "
    "FOR (d:Decision_v2) ON (d.repo, d.status)",
    "CREATE FULLTEXT INDEX codemem_decision_ft IF NOT EXISTS "
    "FOR (d:Decision_v2) ON EACH [d.title, d.body, d.rationale]",

    # Observations: agent / human notes about behaviour, bugs, learnings
    "CREATE CONSTRAINT codemem_observation_unique IF NOT EXISTS "
    "FOR (o:Observation_v2) REQUIRE o.id IS UNIQUE",
    "CREATE INDEX codemem_observation_repo IF NOT EXISTS "
    "FOR (o:Observation_v2) ON (o.repo, o.created_at)",
    "CREATE INDEX codemem_observation_kind IF NOT EXISTS "
    "FOR (o:Observation_v2) ON (o.repo, o.kind)",
    # Dedupe lookup key — short sha256 of text (see memory/store.py);
    # avoids the O(n) {repo, text} scan on every upsert.
    "CREATE INDEX codemem_observation_text_hash IF NOT EXISTS "
    "FOR (o:Observation_v2) ON (o.repo, o.text_hash)",
    "CREATE FULLTEXT INDEX codemem_observation_ft IF NOT EXISTS "
    "FOR (o:Observation_v2) ON EACH [o.text, o.tags_text]",

    # Notes: free-form memos, README-like; lightweight (no embed required)
    "CREATE CONSTRAINT codemem_note_unique IF NOT EXISTS "
    "FOR (n:Note_v2) REQUIRE n.id IS UNIQUE",
    "CREATE INDEX codemem_note_repo IF NOT EXISTS "
    "FOR (n:Note_v2) ON (n.repo, n.created_at)",
    "CREATE FULLTEXT INDEX codemem_note_ft IF NOT EXISTS "
    "FOR (n:Note_v2) ON EACH [n.title, n.body]",

    # ── Doc layer (Doc_v2) — web/external docs ingested into the graph ─
    "CREATE CONSTRAINT codemem_doc_unique IF NOT EXISTS "
    "FOR (d:Doc_v2) REQUIRE d.id IS UNIQUE",
    "CREATE INDEX codemem_doc_repo IF NOT EXISTS "
    "FOR (d:Doc_v2) ON (d.repo, d.source_kind)",
    # NL→file anchoring over raw chunk text (replaces the removed
    # codemem_chunk_embed vector index).
    "CREATE FULLTEXT INDEX codemem_chunk_text_ft IF NOT EXISTS "
    "FOR (c:Chunk_v2) ON EACH [c.text]",
    "CREATE FULLTEXT INDEX codemem_doc_ft IF NOT EXISTS "
    "FOR (d:Doc_v2) ON EACH [d.title, d.body, d.url]",

    # ── Property indices for new emitted fields ────────────────────────
    "CREATE INDEX codemem_file_test_flag IF NOT EXISTS "
    "FOR (f:File_v2) ON (f.repo, f.test_file)",
    "CREATE INDEX codemem_file_lang IF NOT EXISTS "
    "FOR (f:File_v2) ON (f.repo, f.lang)",
    "CREATE INDEX codemem_symbol_visibility IF NOT EXISTS "
    "FOR (s:Symbol_v2) ON (s.repo, s.visibility)",

    # Cross-repo edge — index on (via, confidence) so list-edges queries
    # don't trigger "relationship type unknown" warnings before any
    # CALLS_REPO edges have been written.
    "CREATE INDEX codemem_calls_repo_via IF NOT EXISTS "
    "FOR ()-[r:CALLS_REPO]-() ON (r.via, r.confidence)",

    # Domain / Flow / Tour (features/domain, features/tour) + ReviewFinding
    # (features/review). All keyed per-repo; idempotent MERGE writers.
    "CREATE CONSTRAINT codemem_domain_unique IF NOT EXISTS "
    "FOR (d:Domain) REQUIRE (d.repo, d.name) IS UNIQUE",
    "CREATE CONSTRAINT codemem_flow_unique IF NOT EXISTS "
    "FOR (fl:Flow) REQUIRE (fl.repo, fl.name) IS UNIQUE",
    "CREATE CONSTRAINT codemem_tour_unique IF NOT EXISTS "
    "FOR (t:Tour) REQUIRE (t.repo, t.name) IS UNIQUE",
    "CREATE INDEX codemem_review_repo_kind IF NOT EXISTS "
    "FOR (rf:ReviewFinding) ON (rf.repo, rf.kind)",
]


def _vector_index_statements(dim: int) -> list[str]:
    """Vector indexes for chunk + observation recall. The dimension
    follows the embed sidecar config (AIFORGE_EMBED_DIM, default 1024
    for bge-m3) instead of being hardcoded — see ``embed_config()`` in
    features/chunk/embed.py."""
    return [
        # Vector recall over observations — the ONLY vector index.
        # Code-chunk vectors were removed (2026-06-11): NL→file anchoring
        # now rides the codemem_chunk_text_ft fulltext index; embedding
        # code was the slowest ingest stage and its only consumer was
        # that anchoring path.
        "CREATE VECTOR INDEX codemem_observation_embed IF NOT EXISTS "
        "FOR (o:Observation_v2) ON o.embed_vec "
        f"OPTIONS {{indexConfig: {{`vector.dimensions`: {dim}, "
        "                        `vector.similarity_function`: 'cosine'}}",
    ]


def _embed_dim() -> int:
    """Resolve the embedding dimension from the same config the chunk
    embedder uses. Falls back to the env var directly (then 1024) if
    the feature module can't load — schema apply must never fail on a
    missing optional dependency."""
    try:
        from aiforge_memory.features.chunk.embed import embed_config
        return int(embed_config()["dim"])
    except Exception:  # noqa: BLE001
        return int(os.environ.get("AIFORGE_EMBED_DIM", "1024"))


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

    for stmt in _INDEX_STATEMENTS + _vector_index_statements(_embed_dim()):
        with driver.session() as session:
            session.run(stmt).consume()

    # Migration: drop the removed code-chunk vector index on deployed
    # graphs (idempotent; embed_vec node properties are left in place —
    # harmless dead weight that delta re-ingest gradually rewrites).
    with driver.session() as session:
        session.run("DROP INDEX codemem_chunk_embed IF EXISTS").consume()
