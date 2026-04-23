# graph_rag — code/ops/memory graph for local LLMs

One Neo4j on NUC holds every repo's code, every k8s resource, and every
Claude memory. LM Studio provides embeddings + LLM. MCP server exposes
25 tools to Claude Desktop / CLI / Qwen.

## Live stack

| Part | Where | Port |
|------|-------|------|
| Neo4j 5 + APOC + genai | NUC `192.168.70.191` | 7687 |
| LM Studio (nomic embed + Qwen LLM) | Mac Studio `192.168.70.185` | 1234 |
| SSH tunnel → LM Studio | NUC `lm-tunnel.service` | 1235 |
| MCP server `aiforge-graph` | NUC `aiforge-venv` | stdio over SSH |

## Graph

| Family | Labels |
|--------|--------|
| Code | `Class`, `Method`, `Function`, `Component`, `Endpoint`, `ExternalEndpoint`, `Annotation` |
| Integrations | `MongoCollection`, `NatsSubject`, `KafkaTopic`, `RedisKey` |
| Runtime | `Deployment`, `Service`, `Ingress`, `ConfigMap`, `Secret` (keys only), `PodStatus`, `DockerImage`, `EnvVar` |
| Knowledge | `Memory`, `Repo`, `Package`, `File` |

Key cross-repo edges: `CALLS`, `EXPOSES`, `CALLS_EXTERNAL`, `PUBLISH` /
`SUBSCRIBE`, `PRODUCES` / `CONSUMES`, `READS` / `WRITES` / `DELETES`,
`FLOWS_TO` (producer → consumer via NATS/Kafka/HTTP), `DATA_FLOWS_TO`
(writer → reader via Mongo), `DESCRIBES` (memory → code).

## Pipelines

```
Java / TS / Python repos
   │
   ▼
javaparser (shaded jar)  tsparser (ts-morph)  pyparser (libcst+ast)
   │
   ▼                    JSONL in /tmp/graph_rag/
   │
   └──▶ ingest_jsonl.py ─▶ Neo4j ◀─ ingest_k8s.py ◀─ k8s_sync.py (kubectl)
                           ▲                           ▲
                           │                           │
                     ingest_memory.py ◀── ~/.claude/memory/*.md
                           │
                           ▼
           link_services / link_integrations / link_memories
                           │
                           ▼
               embed_nodes.py (LM Studio nomic 768d)
                           │
                           ▼
                 aiforge-graph-mcp (25 tools)
```

## Run

```bash
# One-shot full rebuild on NUC
cd ~/AIForgeCrew/scripts/graph_rag
bash bin/graph_full_reindex.sh

# Sanity
bash bin/graph_sanity.sh

# Incremental (auto via post-merge hook; manual per repo):
bash bin/graph_incremental.sh <repo-path> <changed-file> ...
```

Overrides: `NEO4J_URI`, `REPOS_ROOT`, `LM_URL`, `EMBED_MODEL`,
`EMBED_DIM`, `NO_RESET=1` to skip phase 0 nuke.

## MCP tools (25)

- Discovery: `sym_lookup`, `list_repos`, `list_services`, `list_endpoints`, `list_integrations`
- Navigation: `graph_neighborhood`, `caller_chain`, `callee_chain`, `read_source`
- Impact: `impact`, `cross_repo_flow`, `data_lineage`
- Build: `build_plan`, `test_plan`, `run_commands`
- K8s: `kube_status`, `kube_describe`, `kube_image_tag`, `kube_config`, `kube_port_forward_cmd`, `kube_rollout_restart` (write, needs `confirm:true`)
- Docs: `find_doc`, `related_memories`
- Tickets: `ticket_fetch`, `ticket_brief`

## Wire into Claude Desktop

```json
"aiforge-graph": {
  "command": "ssh",
  "args": [
    "mani@192.168.70.191",
    "NEO4J_URI=bolt://127.0.0.1:7687 NEO4J_USER=neo4j NEO4J_PASS=password EMBED_URL=http://127.0.0.1:1235/v1 EMBED_MODEL=text-embedding-nomic-embed-text-v1.5 LLM_URL=http://127.0.0.1:1235/v1 LLM_MODEL=qwen3-coder-next /home/mani/aiforge-venv/bin/aiforge-graph-mcp"
  ]
}
```

## Layout

```
graph_rag/
├── config/                 service-map, ticket-url, link-patterns
├── javaparser/             Maven shaded jar
├── tsparser/               ts-morph domain layer
├── pyparser/               libcst + ast domain layer
├── mcp_server/             MCP stdio (25 tools)
├── bin/                    drivers (full_reindex, incremental, sanity, memory_sync)
├── schedulers/             launchd + systemd + git post-merge hook
└── *.py                    ingest_*, link_*, k8s_sync, repo_meta, ticket_*
```

## Safety

- Ingest uses MERGE; only phase-0 of `graph_full_reindex.sh` wipes graph
- K8s writes require `confirm:true`; secrets never leave cluster (keys only)
- Ticket token via env var; never persisted in graph
