"""Impact / blast-radius tools + cross-repo flow trace."""
from __future__ import annotations

from ..cypher_lib import session, IMPACT_CY


def impact(args: dict) -> dict:
    key = args["key"]
    with session() as s:
        rec = s.run(IMPACT_CY, key=key).single()
        return dict(rec["impact"]) if rec else {}


def cross_repo_flow(args: dict) -> dict:
    """For a given integration value (nats subject / kafka topic / mongo coll /
    rest path) return producer(s) and consumer(s) across repos."""
    value = args["value"]
    kind = args.get("kind") or "auto"

    cy_nats = """
    MATCH (s:NatsSubject {subject:$v})
    OPTIONAL MATCH (p:Method)-[:PUBLISH]->(s)
    OPTIONAL MATCH (s)<-[:SUBSCRIBE]-(c:Method)
    RETURN 'nats' AS via, collect(DISTINCT p.fqn) AS producers,
           collect(DISTINCT c.fqn) AS consumers
    """
    cy_kafka = """
    MATCH (t:KafkaTopic {name:$v})
    OPTIONAL MATCH (p:Method)-[:PRODUCES]->(t)
    OPTIONAL MATCH (t)<-[:CONSUMES]-(c:Method)
    RETURN 'kafka' AS via, collect(DISTINCT p.fqn) AS producers,
           collect(DISTINCT c.fqn) AS consumers
    """
    cy_mongo = """
    MATCH (c:MongoCollection {name:$v})
    OPTIONAL MATCH (w:Method)-[:WRITES]->(c)
    OPTIONAL MATCH (r:Method)-[:READS]->(c)
    RETURN 'mongo' AS via, collect(DISTINCT w.fqn) AS producers,
           collect(DISTINCT r.fqn) AS consumers
    """
    cy_http = """
    MATCH (e:Endpoint {path:$v})
    OPTIONAL MATCH (h:Method)-[:EXPOSES]->(e)
    OPTIONAL MATCH (cl:Method)-[:CALLS_EXTERNAL]->(ex:ExternalEndpoint)
      WHERE ex.url ENDS WITH e.path OR ex.url STARTS WITH e.path
    RETURN 'http' AS via, collect(DISTINCT h.fqn) AS producers,
           collect(DISTINCT cl.fqn) AS consumers
    """
    cys = []
    if kind in ("auto", "nats"):
        cys.append(cy_nats)
    if kind in ("auto", "kafka"):
        cys.append(cy_kafka)
    if kind in ("auto", "mongo"):
        cys.append(cy_mongo)
    if kind in ("auto", "http"):
        cys.append(cy_http)

    with session() as s:
        flows = []
        for cy in cys:
            for r in s.run(cy, v=value):
                if r["producers"] or r["consumers"]:
                    flows.append(dict(r))
    return {"value": value, "flows": flows}


def data_lineage(args: dict) -> dict:
    collection = args["collection"]
    field = args.get("field")
    cy = """
    MATCH (c:MongoCollection {name:$coll})
    OPTIONAL MATCH (w:Method)-[:WRITES]->(c)
    OPTIONAL MATCH (r:Method)-[:READS]->(c)
    OPTIONAL MATCH (d:Method)-[:DELETES]->(c)
    RETURN [x IN collect(DISTINCT w) | {fqn:x.fqn, file:x.file}] AS writers,
           [x IN collect(DISTINCT r) | {fqn:x.fqn, file:x.file}] AS readers,
           [x IN collect(DISTINCT d) | {fqn:x.fqn, file:x.file}] AS deleters
    """
    with session() as s:
        r = s.run(cy, coll=collection).single()
        return {"collection": collection, "field": field, **(dict(r) if r else {})}


TOOLS = [
    {
        "name": "impact",
        "description": "Blast radius for a method/function: callers, data readers, "
                       "subscribers, tests, collections/subjects touched.",
        "input_schema": {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
        },
    },
    {
        "name": "cross_repo_flow",
        "description": "Trace producer<->consumer across repos for a NATS subject, "
                       "Mongo collection, or REST path.",
        "input_schema": {
            "type": "object",
            "properties": {
                "value": {"type": "string"},
                "kind": {"type": "string",
                         "enum": ["auto", "nats", "kafka", "mongo", "http"]},
            },
            "required": ["value"],
        },
    },
    {
        "name": "data_lineage",
        "description": "Return writers / readers / deleters of a Mongo collection.",
        "input_schema": {
            "type": "object",
            "properties": {
                "collection": {"type": "string"},
                "field": {"type": "string"},
            },
            "required": ["collection"],
        },
    },
]

HANDLERS = {
    "impact": impact,
    "cross_repo_flow": cross_repo_flow,
    "data_lineage": data_lineage,
}
