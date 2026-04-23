# Graph-RAG PoC (logic-aware)

Parse a Java repo into a Neo4j graph keyed on classes / methods / endpoints /
autowired dependencies / Mongo collections / NATS subjects, then answer
"show me the full flow for endpoint X" questions with a couple of Cypher
queries instead of grep or embedding retrieval.

## Setup (laptop Docker)

```bash
docker rm -f neo4j-poc 2>/dev/null
docker run -d --name neo4j-poc -p 7474:7474 -p 7687:7687 \
    -e NEO4J_AUTH=neo4j/password \
    -e 'NEO4J_PLUGINS=["apoc", "genai"]' \
    neo4j:latest

uv pip install neo4j tree_sitter tree-sitter-java
```

Browser: <http://localhost:7474> (user `neo4j`, password `password`).

## Ingest PosClientBackend

```bash
.venv/bin/python scripts/graph_rag/ingest_java.py \
    --repo ~/Documents/codeRepo/PosClientBackend --reset
```

Current counts (as of 2026-04-23): 1,115 classes, 4,096 methods,
463 endpoints, 27 Mongo collections.

## Query — stock transfer flow

```bash
.venv/bin/python scripts/graph_rag/query.py --topic stockTransfer
```

Returns: endpoints, class list, autowired deps, call graph, inbound
callers, cross-service sync relations (the `PosServerBackendService`
autowire in `StockTransferWorkflow` surfaces here).

Ad-hoc Cypher:

```bash
.venv/bin/python scripts/graph_rag/query.py \
    --free 'MATCH (m:Method)-[:EXPOSES]->(e:Endpoint) WHERE e.path CONTAINS "stockTransfer" RETURN e, m LIMIT 20'
```

## Schema

```
(:Class {fqn, simple, kind, file, package, annotations})
(:Method {fqn, name, sig, file, line, annotations})
(:Endpoint {http, path})
(:MongoCollection {name})
(:NatsSubject {subject})

(Class)-[:CONTAINS]->(Method)
(Class)-[:EXTENDS|IMPLEMENTS]->(Class)
(Class)-[:USES {field}]->(Class)        # autowired / final fields
(Class)-[:BINDS]->(MongoCollection)     # @Document
(Class)-[:PUBLISHES]->(NatsSubject)     # heuristic: subject="..."
(Method)-[:EXPOSES]->(Endpoint)         # @*Mapping
(Method)-[:CALLS {via}]->(Method)       # method_invocation, name-matched
```

## Why Graph-RAG here

Today's Planner uses memory + grep + read_file. For a wide ticket like
"explore stock transfer flow refactoring" it spent 15+ steps reading a
800-line controller before writing a plan. Graph-RAG answers the same
structural questions in one Cypher query:

- `controllers at /stockTransfer` → 9 endpoints in 1 call
- `classes touching this flow` → 7 classes in 1 call
- `cross-service deps` → `StockTransferWorkflow` wires
  `PosServerBackendService` (remote sync) — surfaced without reading code

Next step: feed these query results into the Planner's context bundle
instead of `aiforge-deep-context` grep output.
