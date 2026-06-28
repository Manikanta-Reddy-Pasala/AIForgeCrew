"""Cypher writer for :Tour nodes + STOP edges. Idempotent per (repo, name).

Public surface:
    upsert_tour(driver, *, repo, tour) -> dict counts
"""
from __future__ import annotations

_UPSERT_TOUR = """
MERGE (t:Tour {repo:$repo, name:$name})
SET t.domain = $domain, t.schema_version = 'codemem-v1'
WITH t
MATCH (r:Repo {name:$repo})
MERGE (r)-[:OWNS_TOUR]->(t)
WITH t
OPTIONAL MATCH (t)-[old:STOP]->()
DELETE old
WITH t
UNWIND $stops AS st
  MATCH (y:Symbol_v2 {repo:$repo, fqname:st.node_id})
  MERGE (t)-[rel:STOP {order:st.order}]->(y)
  SET rel.label = st.label, rel.note = st.note
"""
_PRUNE_TOURS = """
MATCH (t:Tour {repo:$repo}) WHERE NOT t.name IN $names DETACH DELETE t
"""


def upsert_tour(driver, *, repo, tour) -> dict:
    with driver.session() as sess:
        sess.run(_UPSERT_TOUR, repo=repo, name=tour.name,
                 domain=tour.domain or "", stops=tour.stops).consume()
    return {"tours": 1, "stops": len(tour.stops)}


def prune_tours(driver, *, repo, keep_names) -> None:
    with driver.session() as sess:
        sess.run(_PRUNE_TOURS, repo=repo, names=list(keep_names)).consume()


__all__ = ["upsert_tour", "prune_tours"]
