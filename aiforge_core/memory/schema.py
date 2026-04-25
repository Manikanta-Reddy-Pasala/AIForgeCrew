"""AIForgeCrew v5 Neo4j schema migration.

Idempotent constraints + indexes for the v5 5-layer memory model:
    L0 :MetaSop      — meta-procedures
    L2 :Fact         — durable facts (vector + fulltext indexed)
    L3 :Sop          — standard operating procedures
    L4 :Session, :Turn — episodic execution traces
    L5 :File, :Symbol — code-graph (with :CALLS, :IMPORTS, :DEFINES,
                       :EXTENDS, :IMPLEMENTS edges)
    plus :Ticket as the L4 root anchor.

This migration coexists with the v4 graph_rag schema (:Class, :Method,
:Endpoint, :Repo, :Memory, k8s nodes). It does NOT drop or rename existing
data — it only adds new constraints/indexes for v5 labels.

Usage::

    from neo4j import GraphDatabase
    from aiforge_core.memory.schema import init_schema, migration_status

    drv = GraphDatabase.driver("bolt://127.0.0.1:7687", auth=("neo4j", "password"))
    init_schema(drv)                       # idempotent
    print(migration_status(drv))           # what's present / missing
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("aiforge.memory.schema")


# ─────────────── config ───────────────

#: Embedding dimension for ``:Fact.embedding``. The seed model is
#: ``nomic-embed-text-v1.5`` (768-d). If the embedding model is swapped
#: later, you must drop & recreate ``factEmbedding`` with the new dim.
FACT_EMBEDDING_DIM = 768
FACT_EMBEDDING_SIMILARITY = "cosine"


@dataclass(frozen=True)
class _ConstraintSpec:
    name: str
    cypher: str


@dataclass(frozen=True)
class _IndexSpec:
    name: str
    cypher: str
    kind: str  # "RANGE" | "VECTOR" | "FULLTEXT"


# ─────────────── constraint definitions ───────────────

CONSTRAINTS: tuple[_ConstraintSpec, ...] = (
    _ConstraintSpec(
        name="ticket_id",
        cypher=(
            "CREATE CONSTRAINT ticket_id IF NOT EXISTS "
            "FOR (t:Ticket) REQUIRE t.id IS UNIQUE"
        ),
    ),
    _ConstraintSpec(
        name="file_path_repo",
        # Composite uniqueness: (path, repo). A given path can recur across
        # repos but is unique within one.
        cypher=(
            "CREATE CONSTRAINT file_path_repo IF NOT EXISTS "
            "FOR (f:File) REQUIRE (f.path, f.repo) IS UNIQUE"
        ),
    ),
    _ConstraintSpec(
        name="symbol_fqn",
        cypher=(
            "CREATE CONSTRAINT symbol_fqn IF NOT EXISTS "
            "FOR (s:Symbol) REQUIRE s.fqn IS UNIQUE"
        ),
    ),
    _ConstraintSpec(
        name="fact_id",
        cypher=(
            "CREATE CONSTRAINT fact_id IF NOT EXISTS "
            "FOR (f:Fact) REQUIRE f.id IS UNIQUE"
        ),
    ),
    _ConstraintSpec(
        name="sop_kind",
        cypher=(
            "CREATE CONSTRAINT sop_kind IF NOT EXISTS "
            "FOR (s:Sop) REQUIRE s.kind IS UNIQUE"
        ),
    ),
    _ConstraintSpec(
        name="metasop_kind",
        cypher=(
            "CREATE CONSTRAINT metasop_kind IF NOT EXISTS "
            "FOR (m:MetaSop) REQUIRE m.kind IS UNIQUE"
        ),
    ),
    _ConstraintSpec(
        name="session_id",
        cypher=(
            "CREATE CONSTRAINT session_id IF NOT EXISTS "
            "FOR (s:Session) REQUIRE s.id IS UNIQUE"
        ),
    ),
)


# ─────────────── index definitions ───────────────

INDEXES: tuple[_IndexSpec, ...] = (
    _IndexSpec(
        name="factEmbedding",
        kind="VECTOR",
        cypher=(
            "CREATE VECTOR INDEX factEmbedding IF NOT EXISTS "
            "FOR (f:Fact) ON (f.embedding) "
            "OPTIONS {indexConfig: {"
            f"  `vector.dimensions`: {FACT_EMBEDDING_DIM}, "
            f"  `vector.similarity_function`: '{FACT_EMBEDDING_SIMILARITY}' "
            "}}"
        ),
    ),
    _IndexSpec(
        name="factText",
        kind="FULLTEXT",
        cypher=(
            "CREATE FULLTEXT INDEX factText IF NOT EXISTS "
            "FOR (f:Fact) ON EACH [f.text]"
        ),
    ),
    _IndexSpec(
        name="symbol_file_path",
        kind="RANGE",
        cypher=(
            "CREATE INDEX symbol_file_path IF NOT EXISTS "
            "FOR (s:Symbol) ON (s.file_path)"
        ),
    ),
    _IndexSpec(
        name="fact_source",
        kind="RANGE",
        cypher=(
            "CREATE INDEX fact_source IF NOT EXISTS "
            "FOR (f:Fact) ON (f.source)"
        ),
    ),
    _IndexSpec(
        name="fact_tags",
        kind="RANGE",
        cypher=(
            "CREATE INDEX fact_tags IF NOT EXISTS "
            "FOR (f:Fact) ON (f.tags)"
        ),
    ),
    _IndexSpec(
        name="session_ticket_id",
        kind="RANGE",
        cypher=(
            "CREATE INDEX session_ticket_id IF NOT EXISTS "
            "FOR (s:Session) ON (s.ticket_id)"
        ),
    ),
)


# ─────────────── public API ───────────────

@dataclass
class MigrationResult:
    """Summary of a single ``init_schema`` run."""

    constraints_created: list[str] = field(default_factory=list)
    constraints_existing: list[str] = field(default_factory=list)
    constraints_failed: list[tuple[str, str]] = field(default_factory=list)
    indexes_created: list[str] = field(default_factory=list)
    indexes_existing: list[str] = field(default_factory=list)
    indexes_failed: list[tuple[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "constraints_created": self.constraints_created,
            "constraints_existing": self.constraints_existing,
            "constraints_failed": self.constraints_failed,
            "indexes_created": self.indexes_created,
            "indexes_existing": self.indexes_existing,
            "indexes_failed": self.indexes_failed,
        }


def _existing_constraint_names(session) -> set[str]:
    return {row["name"] for row in session.run("SHOW CONSTRAINTS YIELD name")}


def _existing_index_names(session) -> set[str]:
    return {row["name"] for row in session.run("SHOW INDEXES YIELD name")}


def init_schema(driver) -> MigrationResult:
    """Create v5 constraints + indexes. Idempotent — safe to re-run.

    Existing v4 data and schema are not touched. Failures are captured in
    the result rather than raised, so a partial migration still proceeds.
    """
    result = MigrationResult()
    with driver.session() as session:
        before_constraints = _existing_constraint_names(session)
        before_indexes = _existing_index_names(session)

        for c in CONSTRAINTS:
            if c.name in before_constraints:
                result.constraints_existing.append(c.name)
                continue
            try:
                session.run(c.cypher)
                result.constraints_created.append(c.name)
                log.info("schema.constraint_created", extra={"name": c.name})
            except Exception as exc:  # pragma: no cover — server reports errors
                result.constraints_failed.append((c.name, str(exc)))
                log.warning("schema.constraint_failed name=%s err=%s", c.name, exc)

        for idx in INDEXES:
            if idx.name in before_indexes:
                result.indexes_existing.append(idx.name)
                continue
            try:
                session.run(idx.cypher)
                result.indexes_created.append(idx.name)
                log.info(
                    "schema.index_created", extra={"name": idx.name, "kind": idx.kind}
                )
            except Exception as exc:  # pragma: no cover
                result.indexes_failed.append((idx.name, str(exc)))
                log.warning(
                    "schema.index_failed name=%s kind=%s err=%s",
                    idx.name,
                    idx.kind,
                    exc,
                )
    return result


def migration_status(driver) -> dict[str, Any]:
    """Return which v5 constraints/indexes are present vs. missing.

    Useful for ops tooling. Does not modify the database.
    """
    expected_constraints = {c.name for c in CONSTRAINTS}
    expected_indexes = {idx.name for idx in INDEXES}
    with driver.session() as session:
        present_constraints = _existing_constraint_names(session)
        present_indexes = _existing_index_names(session)
    return {
        "constraints": {
            "present": sorted(expected_constraints & present_constraints),
            "missing": sorted(expected_constraints - present_constraints),
        },
        "indexes": {
            "present": sorted(expected_indexes & present_indexes),
            "missing": sorted(expected_indexes - present_indexes),
        },
        "fact_embedding_dim": FACT_EMBEDDING_DIM,
        "fact_embedding_similarity": FACT_EMBEDDING_SIMILARITY,
    }
