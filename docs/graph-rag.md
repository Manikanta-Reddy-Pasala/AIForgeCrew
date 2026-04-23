# Graph + Vector DB (how it works)

One Neo4j on the NUC holds every repo, every k8s resource, and every memory
note as a **logic-aware graph** with a **vector index on each node's text**.
Four retrieval layers coexist in the same DB: vector, BM25, graph traversal,
rerank.

## The graph in one picture

```
    ┌──────── Java / Node / Python repos ────────┐
    │                                             │
 JavaParser jar       ts-morph (tsparser)     libcst (pyparser)
    │                     │                        │
    └─────────────────────┼────────────────────────┘
                          ▼
               (:Class) (:Method) (:Function) (:Component)
                          │
       CALLS / PARAM_TYPE / RETURNS / IMPORTS / ANNOTATED
                          │
                          ▼
               (:Endpoint) (:MongoCollection) (:NatsSubject)
               (:ExternalEndpoint) (:EnvVar) (:Annotation)
                          │
                          ▼   linker
               FLOWS_TO  across repos via NATS subject
                          / Mongo collection / REST path
                          ▼
                   k8s (:Deployment) ── RUNS_IMAGE ── (:DockerImage)
                          │
                          ▼
          ~/.claude/memory/*.md ── DESCRIBES ──> target code nodes
```

Every node also gets a 1024-dim `bge-m3` embedding so
`db.index.vector.queryNodes(...)` finds code by intent, not by identifier.

## What lives in Neo4j

Six families of nodes, ~30 edge types. See `scripts/graph_rag/README.md` for
the full table. Key ones:

- **Code**: `Class`, `Method`, `Function`, `Field`, `Symbol` (SCIP), `Component`
  (React), `Endpoint`, `ExternalEndpoint`
- **Integrations**: `MongoCollection`, `NatsSubject`, `KafkaTopic`, `RedisKey`
- **Runtime**: `Deployment`, `Service`, `Ingress`, `DockerImage`, `PodStatus`
- **Config / Context**: `EnvVar`, `ConfigKey`, `FeatureToggle`
- **Knowledge**: `Memory` (claude-memory md), `Ticket`, `Repo`, `Package`

## How "ticket → context" happens

`ticket_brief` MCP tool (one call) returns a ~12k-token pack:

1. **Hybrid search** the ticket title + body against every node's `bge-m3`
   embedding + BM25 index, rerank via `bge-reranker-v2-m3`.
2. **Graph-expand** the top hits: 1–2 hops of `CALLS`, `EXPOSES`,
   `READS`/`WRITES`, `FLOWS_TO` — so the LLM sees the whole neighbourhood,
   not just the single closest method.
3. **Build pack**: candidate services, primary symbols with file:line,
   upstream callers, Mongo/NATS surfaces touched, k8s deployment + tag,
   related memories.

That pack is what the Planner and Doer read instead of grepping files.

## Indexing pipelines

Source-of-truth extractors → JSONL → ingester → Neo4j + vector index.

| Source | Extractor | Notes |
|--------|-----------|-------|
| Java repos | `javaparser/` shaded jar | classes, methods, calls, signatures, Spring annotations, Mongo string scans, NATS subjects |
| Node / TS / React | `tsparser/` (ts-morph) | components, hooks, express, nest, fetch URLs |
| Python | `pyparser/` (libcst + ast) | FastAPI / Flask routes, pymongo, nats-py, env reads |
| k8s | `ingest_k8s.py` (READ-only kubectl) | deployments, services, config maps, image tags |
| Memory | `ingest_memory.py` | `~/.claude/memory/*.md` → `(:Memory)` w/ `DESCRIBES` links |

Triggered by:
- `aiforge-file-indexer.timer` (30 min) — incremental by SHA
- `aiforge-reindex-daily.timer` (02:00) — canon wings full rebuild
- Post-commit hook on `~/codeRepo/*` (when set up)

## Retrieval surface

Exposed to the LLM via MCP stdio (`mcp_server/`, ~18 tools):

- `sym_lookup` — hybrid search
- `graph_neighborhood` / `caller_chain` / `callee_chain` — traversal
- `impact` — blast radius for a change
- `cross_repo_flow` / `data_lineage` — producer↔consumer via NATS/Mongo/REST
- `build_plan` / `test_plan` — ordered commands per repo
- `kube_status` / `kube_image_tag` — read-only cluster lookup
- `related_memories` — memory search

See `scripts/graph_rag/README.md` for the full tool list + schema.
