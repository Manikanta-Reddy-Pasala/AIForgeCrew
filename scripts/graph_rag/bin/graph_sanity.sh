#!/usr/bin/env bash
# Sanity counts + acceptance queries. Run after a reindex.
set -euo pipefail

NEO4J_URI="${NEO4J_URI:-bolt://127.0.0.1:7687}"
NEO4J_USER="${NEO4J_USER:-neo4j}"
NEO4J_PASS="${NEO4J_PASS:-password}"

cypher() {
  cypher-shell -a "$NEO4J_URI" -u "$NEO4J_USER" -p "$NEO4J_PASS" --format plain "$1"
}

echo "== Node counts =="
cypher 'MATCH (n) RETURN labels(n)[0] AS label, count(*) AS n ORDER BY n DESC LIMIT 30'

echo
echo "== Relationship counts =="
cypher 'MATCH ()-[r]->() RETURN type(r) AS rel, count(*) AS n ORDER BY n DESC LIMIT 30'

echo
echo "== Repos indexed =="
cypher 'MATCH (r:Repo) RETURN r.name, r.lang ORDER BY r.name'

echo
echo "== Services mapped =="
cypher 'MATCH (r:Repo)-[:IS_SERVICE]->(d:Deployment) RETURN r.name, d.env_label, d.ns, d.name'

echo
echo "== Cross-repo NATS flows =="
cypher '
MATCH (p:Method)-[f:FLOWS_TO {via:"nats"}]->(c:Method)
RETURN f.subject, p.fqn AS producer, c.fqn AS consumer
LIMIT 10'

echo
echo "== Memories linked to code =="
cypher 'MATCH (m:Memory)-[:DESCRIBES]->(t) RETURN labels(t)[0] AS kind, count(*) AS n ORDER BY n DESC'
