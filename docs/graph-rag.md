# Graph + Vector DB

One Neo4j on NUC holds every repo, every k8s resource, and every memory.
Four retrieval layers in one DB: vector, BM25, graph, rerank (optional).

## Stack

| Part | Where | Port |
|------|-------|------|
| Neo4j 5 + APOC + genai | NUC `192.168.70.191` | 7687 |
| LM Studio (embed + LLM) | Mac Studio `192.168.70.185` | 1234 |
| Tunnel NUC→LM Studio | systemd `lm-tunnel.service` | :1235 → :1234 |
| Embedder | `text-embedding-nomic-embed-text-v1.5` (768d) | via LM Studio |
| LLM | `qwen3-coder-next` (256K ctx) | via LM Studio |
| Reranker | none — LM Studio skips; callers fall back to vector order |

Implementation at `scripts/graph_rag/` — see its `README.md` for full
tool list + schema.

## The graph in one picture

```
    ┌──────── Java / Node / Python repos ────────┐
    │                                             │
 JavaParser jar   ts-morph (tsparser)    libcst (pyparser)
    │                     │                    │
    └─────────────────────┼────────────────────┘
                          ▼
              (:Class) (:Method) (:Function) (:Component)
                          │
        CALLS / PARAM_TYPE / RETURNS / IMPORTS / ANNOTATED
                          │
                          ▼
           (:Endpoint) (:MongoCollection) (:NatsSubject) (:KafkaTopic)
           (:ExternalEndpoint) (:EnvVar) (:Annotation)
                          │
                          ▼   linker
           FLOWS_TO across repos via NATS / Kafka / REST
           DATA_FLOWS_TO across repos via Mongo
                          │
                          ▼
         k8s (:Deployment) ─ RUNS_IMAGE ─ (:DockerImage)
         (:Service) ─ TARGETS ─ (:Deployment) ◀─ ROUTES ─ (:Ingress)
                          │
                          ▼
     ~/.claude/memory/*.md ─ DESCRIBES ─▶ code/ops nodes
```

Every ingestable node gets a nomic embedding so
`db.index.vector.queryNodes(...)` finds code by intent, not identifier.

## Node families

| Family | Labels |
|--------|--------|
| Code | `Class`, `Method`, `Function`, `Component`, `Endpoint`, `ExternalEndpoint`, `Annotation`, `Symbol` (SCIP) |
| Integrations | `MongoCollection`, `NatsSubject`, `KafkaTopic`, `RedisKey` |
| Runtime | `Deployment`, `Service`, `Ingress`, `ConfigMap`, `Secret` (keys only), `PodStatus`, `DockerImage`, `EnvVar` |
| Knowledge | `Memory`, `Ticket`, `Repo`, `Package`, `File` |

## Ticket → context

`ticket_brief` MCP tool (one call) returns a pack:

1. Hybrid search ticket text against every node's nomic embedding + Neo4j
   fulltext (BM25)
2. Graph-expand top hits — 1–2 hops of `CALLS`, `EXPOSES`, `READS/WRITES`,
   `FLOWS_TO`
3. Attach impact: upstream callers, Mongo/NATS/Kafka touched, k8s image
   tag, related memories

Planner and Doer agents consume that pack instead of grepping files.

## Indexing

| Source | Extractor | Triggers |
|--------|-----------|----------|
| Java repos | `javaparser/` (shaded jar) | Full reindex; post-merge hook |
| Node / TS / React | `tsparser/` (ts-morph) | Same |
| Python | `pyparser/` (libcst + ast) | Same |
| k8s | `k8s_sync.py` (READ-only via kubectl) | Manual / launchd 15m |
| Memory | `ingest_memory.py` | Manual; post-merge on memory-repo |

Full rebuild: `bash bin/graph_full_reindex.sh` on NUC.
Incremental: git `post-merge` hook fires `bin/graph_incremental.sh`.

## MCP tools (25)

Exposed via stdio at `aiforge-graph-mcp`. Wired into smolagents
(LangGraph Planner + Doer nodes) through
`aiforge_core.mcp_graph.graph_rag_tools` — opt-in via env flag.

```bash
# On NUC (graph-runner process co-located with MCP server)
export AIFORGE_GRAPH_MCP_ENABLED=1
export AIFORGE_GRAPH_MCP_BIN=/home/mani/aiforge-venv/bin/aiforge-graph-mcp

# On Mac Studio (reach NUC over ssh)
export AIFORGE_GRAPH_MCP_ENABLED=1
export AIFORGE_GRAPH_MCP_HOST=mani@192.168.70.191
```

LangGraph node → smolagents `ToolCallingAgent` → `make_tools()` merges
local file tools + graph_rag MCP tools into one list handed to the LLM.

- Discovery: `sym_lookup`, `list_{repos,services,endpoints,integrations}`
- Navigation: `graph_neighborhood`, `caller_chain`, `callee_chain`, `read_source`
- Impact: `impact`, `cross_repo_flow`, `data_lineage`
- Build: `build_plan`, `test_plan`, `run_commands`
- K8s (read): `kube_status`, `kube_describe`, `kube_image_tag`, `kube_config`, `kube_port_forward_cmd`
- K8s (write): `kube_rollout_restart` — requires `confirm:true`
- Docs: `find_doc`, `related_memories`
- Tickets: `ticket_fetch`, `ticket_brief`

## Current counts (2026-04-24)

| Nodes | Count |
|---|---|
| Method | 22,814 |
| Class | 7,801 |
| Endpoint | 2,377 |
| MongoCollection | 158 |
| KafkaTopic | ~57 |
| NatsSubject | 2 |
| Deployment | 45 |
| Service | 33 |
| Memory | 23 |
| Repo | 39 |

| Cross-repo edges | Count |
|---|---|
| HTTP FLOWS_TO | 2,482 |
| Mongo DATA_FLOWS_TO | 647 |
| Kafka FLOWS_TO | 32 |
| NATS FLOWS_TO | 1 |
| Memory DESCRIBES | 81 |

## Not here

- Metrics — skipped
- Logs — skipped
- Tekton state — skipped
- bge-m3 / bge-reranker — reachable endpoint TBD; currently nomic only
- Prod k8s cert rotated 2026-04-24 (fresh cert valid to 2027-01); QA + prod both indexed
