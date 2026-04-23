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

Current counts (as of 2026-04-23 ingest v2 on PosClientBackend):

```
Classes: 1,135  Methods: 4,144  Endpoints: 463  Packages: 200
MongoCollections: 37  ExternalEndpoints: 3

CALLS: 11,824  CONTAINS: 4,144  USES: 3,485  IMPORTS: 2,744
CONTAINS_CLASS: 1,066  EXPOSES: 463  IMPLEMENTS: 119
READS: 83  WRITES: 17  EXTENDS: 8  CALLS_EXTERNAL: 5
```

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

## Schema (v2)

```
(:Package {name})
(:Class  {fqn, simple, kind, file, package, layer, loc,
          annotations, imports, javadoc,
          transactional, async, scheduled, cacheable})
(:Method {fqn, name, sig, file, line, loc, return_type,
          body_snippet, javadoc, annotations,
          transactional, async, scheduled, cacheable})
(:Endpoint {http, path, params, method_fqn})
(:MongoCollection {name})
(:NatsSubject {subject})
(:ExternalEndpoint {url})

(Package)-[:CONTAINS_CLASS]->(Class)
(Class)-[:CONTAINS]->(Method)
(Class)-[:EXTENDS|IMPLEMENTS]->(Class)
(Class)-[:USES {field}]->(Class)          # autowired / final fields
(Class)-[:IMPORTS]->(Class)               # import statements resolved in-repo
(Class)-[:BINDS]->(MongoCollection)       # @Document
(Class)-[:PUBLISHES]->(NatsSubject)       # legacy heuristic
(Method)-[:EXPOSES]->(Endpoint)           # @*Mapping
(Method)-[:CALLS {via, certainty}]->(Method)
                                          # certainty: resolved | name_only
(Method)-[:READS|WRITES|DELETES]->(MongoCollection)
                                          # mongoTemplate + Spring Data repo calls
(Method)-[:CALLS_EXTERNAL]->(ExternalEndpoint)
                                          # WebClient.uri / restTemplate
(Method)-[:PUBLISH|SUBSCRIBE]->(NatsSubject)
                                          # publish(..) / subscribe(..) in bodies
```

### v2 additions over v1

- **Layer tagging** on Class (controller | service | repository | workflow |
  mapper | model | config | component | other) from annotations + name
  conventions.
- **Method body snippets** (first 2.5 KB) + **javadoc** stored as properties
  — gives an LLM a "read this function" hit without going to disk.
- **Return type** + **LOC** per method — triage signal ("how big is this").
- **Imports** as edges when the imported class is in-repo → enables transitive
  dependency graphs by actual types, not just autowired fields.
- **Type-resolved CALLS**: when a call's receiver is an autowired field or
  local variable whose type we know, the edge is marked `certainty: resolved`
  and pointed at the unique target. Ambiguous calls fall back to
  `certainty: name_only` with a fanout cap.
- **Mongo R/W/D edges** from Method to MongoCollection — detects
  `mongoTemplate.find*/save*/update*/delete*` and Spring Data repository
  method name patterns (`findBy*` = READ, `save*` = WRITE, `delete*` =
  DELETE) against the `@Document` collection-hint index built in pass 1.
- **External endpoints** (WebClient `.uri("...")`, `restTemplate.*For*`).
- **Spring flags** on both Class and Method: `transactional`, `async`,
  `scheduled`, `cacheable`.
- **Canned queries** in `query.py`: `classes`, `endpoints`, `deps_out`,
  `deps_in`, `callgraph`, `inbound_calls`, `mongo_rw`, `external`, `nats`,
  `cross_service`, `fanout`, `ctx_brief`, `transactional`.
- **Batched writes** via UNWIND everywhere + indexes on `Class.simple`,
  `Method.name`, `Endpoint.path`, `Class.layer` for fast topic lookups.

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
