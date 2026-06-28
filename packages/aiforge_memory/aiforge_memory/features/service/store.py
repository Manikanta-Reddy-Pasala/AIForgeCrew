"""Cypher writer for Service nodes + OWNS_SERVICE/CONTAINS_FILE edges.

Public surface:
    upsert_services(driver, *, repo, services) -> dict counts

Pre-condition: a (:Repo {name: repo}) exists (Stage 2 ran).

Idempotent: MERGE on (Service.repo, Service.name); MERGE edges.
File nodes are placeholders here — they're created with only
(repo, path) keys so that Stage 4 (plan 3) can later fill in
hash, lang, lines, summary, ... without changing identity.

Stale CONTAINS_FILE edges are removed (i.e. if a re-ingest moves
a file out of a service, the old edge is dropped). Stale Service
nodes for the repo (services that disappeared) are deleted along
with their OWNS_SERVICE edges.
"""
from __future__ import annotations

from aiforge_memory.features.service.extract import ServiceDraft

_UPSERT_SERVICE = """
MERGE (s:Service {repo: $repo, name: $name})
SET s.description = $description,
    s.role        = $role,
    s.tech_stack  = $tech_stack,
    s.port        = $port,
    s.source      = $source,
    s.schema_version = 'codemem-v1'
WITH s
MATCH (r:Repo {name: $repo})
MERGE (r)-[:OWNS_SERVICE]->(s)
"""

_UPSERT_FILE_EDGE = """
MERGE (f:File_v2 {repo: $repo, path: $path})
ON CREATE SET f.schema_version = 'codemem-v1'
WITH f
MATCH (s:Service {repo: $repo, name: $name})
MERGE (s)-[:CONTAINS_FILE]->(f)
"""

# Drop CONTAINS_FILE edges to files no longer in the service's file list.
_PRUNE_STALE_EDGES = """
MATCH (s:Service {repo: $repo, name: $name})-[r:CONTAINS_FILE]->(f:File_v2)
WHERE NOT f.path IN $files
DELETE r
"""

# Drop services that disappeared on re-ingest.
_PRUNE_STALE_SERVICES = """
MATCH (s:Service {repo: $repo})
WHERE NOT s.name IN $names
DETACH DELETE s
"""


def upsert_services(
    driver,
    *,
    repo: str,
    services: list[ServiceDraft],
) -> dict:
    counts = {"services": 0, "file_edges": 0, "pruned_services": 0}
    names = [s.name for s in services]

    with driver.session() as sess:
        # 1. drop services that no longer exist for this repo
        result = sess.run(_PRUNE_STALE_SERVICES, repo=repo, names=names)
        counts["pruned_services"] = result.consume().counters.nodes_deleted

        # 2. upsert each service + edges
        for svc in services:
            sess.run(
                _UPSERT_SERVICE,
                repo=repo,
                name=svc.name,
                description=svc.description,
                role=svc.role,
                tech_stack=svc.tech_stack,
                port=svc.port,
                source=svc.source,
            ).consume()
            counts["services"] += 1

            # prune file edges that are no longer claimed by this service
            sess.run(
                _PRUNE_STALE_EDGES,
                repo=repo, name=svc.name, files=svc.files,
            ).consume()

            for path in svc.files:
                sess.run(
                    _UPSERT_FILE_EDGE,
                    repo=repo, name=svc.name, path=path,
                ).consume()
                counts["file_edges"] += 1

    return counts
