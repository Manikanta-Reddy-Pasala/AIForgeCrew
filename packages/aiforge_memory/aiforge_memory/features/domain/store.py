"""Cypher writer for :Domain + :Flow nodes and their edges.

Public surface:
    upsert_domains(driver, *, repo, domains, flows) -> dict counts

Pre-condition: (:Repo {name: repo}) + its :Service/:Symbol_v2 exist.
Idempotent: MERGE on (repo, name); stale Domains/Flows for the repo are
pruned so a re-run reflects the current graph.
"""
from __future__ import annotations

_UPSERT_DOMAIN = """
MERGE (d:Domain {repo:$repo, name:$name})
SET d.description = $description, d.schema_version = 'codemem-v1'
WITH d
MATCH (r:Repo {name:$repo})
MERGE (r)-[:OWNS_DOMAIN]->(d)
WITH d
UNWIND $services AS svc
  MATCH (s:Service {repo:$repo, name:svc})
  MERGE (d)-[:COVERS]->(s)
WITH DISTINCT d
UNWIND $key_symbols AS ks
  MATCH (y:Symbol_v2 {repo:$repo, fqname:ks})
  MERGE (d)-[:KEY_SYMBOL]->(y)
"""

# Prune stale COVERS / KEY_SYMBOL edges no longer in the draft.
_PRUNE_DOMAIN_EDGES = """
MATCH (d:Domain {repo:$repo, name:$name})-[r:COVERS]->(s:Service)
WHERE NOT s.name IN $services DELETE r
"""
_PRUNE_DOMAIN_KEYS = """
MATCH (d:Domain {repo:$repo, name:$name})-[r:KEY_SYMBOL]->(y:Symbol_v2)
WHERE NOT y.fqname IN $key_symbols DELETE r
"""

_UPSERT_FLOW = """
MERGE (fl:Flow {repo:$repo, name:$name})
SET fl.description = $description, fl.schema_version = 'codemem-v1'
WITH fl
MATCH (r:Repo {name:$repo})
MERGE (r)-[:OWNS_FLOW]->(fl)
WITH fl
// drop old steps so order stays consistent on re-run
OPTIONAL MATCH (fl)-[old:STEP]->()
DELETE old
WITH fl
UNWIND $steps AS st
  MATCH (y:Symbol_v2 {repo:$repo, fqname:st.node_id})
  MERGE (fl)-[rel:STEP {order:st.order}]->(y)
  SET rel.label = st.label
"""

_PRUNE_DOMAINS = """
MATCH (d:Domain {repo:$repo}) WHERE NOT d.name IN $names DETACH DELETE d
"""
_PRUNE_FLOWS = """
MATCH (fl:Flow {repo:$repo}) WHERE NOT fl.name IN $names DETACH DELETE fl
"""


def upsert_domains(driver, *, repo, domains, flows) -> dict:
    dn = 0
    fn = 0
    with driver.session() as sess:
        for d in domains:
            sess.run(_UPSERT_DOMAIN, repo=repo, name=d.name,
                     description=d.description, services=d.services,
                     key_symbols=d.key_symbols).consume()
            sess.run(_PRUNE_DOMAIN_EDGES, repo=repo, name=d.name,
                     services=d.services).consume()
            sess.run(_PRUNE_DOMAIN_KEYS, repo=repo, name=d.name,
                     key_symbols=d.key_symbols).consume()
            dn += 1
        for f in flows:
            sess.run(_UPSERT_FLOW, repo=repo, name=f.name,
                     description=f.description, steps=f.steps).consume()
            fn += 1
        sess.run(_PRUNE_DOMAINS, repo=repo,
                 names=[d.name for d in domains]).consume()
        sess.run(_PRUNE_FLOWS, repo=repo,
                 names=[f.name for f in flows]).consume()
    return {"domains": dn, "flows": fn}


__all__ = ["upsert_domains"]
