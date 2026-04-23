# graph_rag

Neo4j on NUC holding every repo's code, every k8s resource (qa + prod),
every Claude memory. LM Studio on Mac Studio serves embed + LLM. MCP
server exposes 25 tools.

## Stack

| Part | Where | Port |
|---|---|---|
| Neo4j 5 (APOC + genai) | NUC `192.168.70.191` | 7687 |
| LM Studio (nomic + Qwen) | Mac Studio `192.168.70.185` | 1234 |
| Tunnel NUC→LM Studio | `lm-tunnel.service` | :1235 → :1234 |
| MCP `aiforge-graph` | NUC `aiforge-venv` | stdio over ssh |

## Graph nodes

- **Code**: `Class` `Method` `Function` `Component` `Endpoint` `ExternalEndpoint` `Annotation`
- **Integrations**: `MongoCollection` `NatsSubject` `KafkaTopic` `RedisKey`
- **Runtime**: `Deployment` `Service` `Ingress` `ConfigMap` `Secret` (keys only) `PodStatus` `DockerImage` `EnvVar`
- **Knowledge**: `Memory` `Repo` `Package` `File`

## Edges

`CALLS` `EXPOSES` `CALLS_EXTERNAL` `PUBLISH` `SUBSCRIBE` `PRODUCES`
`CONSUMES` `READS` `WRITES` `DELETES` `FLOWS_TO` (cross-repo NATS/Kafka/
HTTP) `DATA_FLOWS_TO` (cross-repo Mongo) `DESCRIBES` (memory → code).

## Pipeline

```
repos (Java/TS/Python)
   ↓ javaparser / tsparser / pyparser
JSONL
   ↓ ingest_jsonl.py                       ingest_k8s.py ← k8s_sync.py
   ↓                                       ingest_memory.py ← ~/.claude/memory
Neo4j ← link_services / link_integrations / link_memories
   ↓ embed_nodes.py --extras (LM Studio nomic 768d)
   ↓
aiforge-graph-mcp (25 tools)
```

## Run

```bash
cd ~/AIForgeCrew/scripts/graph_rag       # on NUC
bash bin/graph_full_reindex.sh           # full rebuild
bash bin/graph_sanity.sh                 # counts + test queries
bash bin/graph_incremental.sh <repo> <file>...   # delta
```

Env overrides: `NEO4J_URI`, `REPOS_ROOT`, `LM_URL`, `EMBED_MODEL`,
`EMBED_DIM`, `NO_RESET=1` skips phase-0 nuke.

## MCP tools

Discovery `sym_lookup` `list_repos` `list_services` `list_endpoints`
`list_integrations` · Navigation `graph_neighborhood` `caller_chain`
`callee_chain` `read_source` · Impact `impact` `cross_repo_flow`
`data_lineage` · Build `build_plan` `test_plan` `run_commands` · K8s
`kube_status` `kube_describe` `kube_image_tag` `kube_config`
`kube_port_forward_cmd` `kube_rollout_restart` (write,
`confirm:true`) · Docs `find_doc` `related_memories` · Tickets
`ticket_fetch` `ticket_brief`.

## Wire to Claude Desktop

```json
"aiforge-graph": {
  "command": "ssh",
  "args": ["mani@192.168.70.191",
    "NEO4J_URI=bolt://127.0.0.1:7687 NEO4J_USER=neo4j NEO4J_PASS=password EMBED_URL=http://127.0.0.1:1235/v1 EMBED_MODEL=text-embedding-nomic-embed-text-v1.5 LLM_URL=http://127.0.0.1:1235/v1 LLM_MODEL=qwen3-coder-next /home/mani/aiforge-venv/bin/aiforge-graph-mcp"]
}
```

## Auto-update

- NUC `aiforge-repo-pull.timer` pulls code every 5 min
- NUC `aiforge-memory-pull.timer` pulls memory repo every 5 min
- 43 × `post-merge` hooks fire `graph_incremental.sh` on pull
- Laptop `com.aiforge.k8s-sync` launchd: qa + prod snapshot every 15 min
- NUC `aiforge-reindex-daily.timer` full rebuild at 02:00

See [`docs/auto-update.md`](../../docs/auto-update.md).

## Safety

MERGE-only ingest (phase-0 is the only wipe) · secrets stored as keys
only, values never leave cluster · k8s writes need `confirm:true` ·
ticket token via env var.

## Layout

```
graph_rag/
├── config/          service-map, ticket-url, link-patterns
├── javaparser/      Maven shaded jar
├── tsparser/        ts-morph domain layer
├── pyparser/        libcst + ast domain layer
├── mcp_server/      MCP stdio (25 tools)
├── bin/             full_reindex / incremental / sanity / memory_sync
├── schedulers/      launchd + systemd + post-merge hook
└── *.py             ingest_*, link_*, k8s_sync, repo_meta, ticket_*
```
