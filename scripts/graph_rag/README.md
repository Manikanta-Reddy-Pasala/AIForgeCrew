# graph_rag — cross-repo code graph + vector RAG

Ingests every repo (Java, Node/TS/React, Python), every k8s deployment,
every Claude memory into a single Neo4j graph + vector index. MCP server
exposes ~18 tools that let a local LLM (Qwen via LM Studio) answer ticket
questions with precise file:line citations, impact blast-radius, and full
build/test/deploy context.

## Architecture

```
┌─────────────────── Qwen (LM Studio :1234) ───────────────────┐
│                                                               │
│        MCP stdio: aiforge-graph (~18 tools)                   │
│                                                               │
│  ┌─────┴──────┐  ┌────────┐  ┌──────────┐  ┌──────────────┐   │
│  │  Neo4j 5   │  │ bge-m3 │  │  bge-    │  │ k8s READ     │   │
│  │  APOC +    │  │  :8764 │  │ reranker │  │ (kubectl)    │   │
│  │  genai     │  │ (embed)│  │  :8765   │  │              │   │
│  │ (graph +   │  │  TEI   │  │   TEI    │  └──────────────┘   │
│  │  vector +  │  └────────┘  └──────────┘                      │
│  │  BM25)     │                                                │
│  └─────┬──────┘                                                │
│        │ Cypher UNWIND                                         │
│  ┌─────┴────────────────────────────────────────────────┐      │
│  │ Ingest:                                              │      │
│  │   SCIP indexers (scip-java / ts / python) —          │      │
│  │     cross-language symbols + calls + refs            │      │
│  │   JavaParser domain — Spring annotations, body,      │      │
│  │     NATS/Mongo string scan                            │      │
│  │   tsparser (ts-morph) — React, express, nest,        │      │
│  │     fetch URLs, mongoose                              │      │
│  │   pyparser (libcst+ast) — FastAPI/Flask, pymongo,    │      │
│  │     nats-py, env vars                                 │      │
│  │   k8s_sync — Deployment/Service/Ingress/ConfigMap/   │      │
│  │     Secret(keys only)/CronJob/PodStatus               │      │
│  │   ingest_memory — ~/.claude/projects/*/memory/*.md   │      │
│  └──────────────────────────────────────────────────────┘      │
└────────────────────────────────────────────────────────────────┘
```

### Sidecars, no more, no less

| Purpose | Tool | Port / Path |
|---|---|---|
| Graph + vector + BM25 | Neo4j 5 + APOC + genai plugin | 7474 / 7687 |
| Embeddings | HuggingFace TEI `bge-m3` (1024d) | 8764 |
| Reranker | HuggingFace TEI `bge-reranker-v2-m3` | 8765 |
| LLM | LM Studio `qwen3-coder-next @ 256K` | 1234 |

No Qdrant, no Chroma, no LlamaIndex, no GraphRAG framework — Neo4j covers
all four retrieval layers (vector, BM25, graph, rerank piggybacked via TEI).

## Graph schema

### Nodes

| Label | Origin |
|---|---|
| `Repo` `File` `Package` `Module` | repo_meta + extractors |
| `Class` `Method` `Function` `Component` `Field` | extractors |
| `Symbol` | SCIP (cross-lang) |
| `Test` `Annotation` `Dependency` | extractors |
| `Endpoint` `ExternalEndpoint` | extractors (Spring/Nest/express/FastAPI/Flask) |
| `MongoCollection` `MongoIndex` `Document` | extractors |
| `NatsSubject` `NatsStream` `KafkaTopic` | extractors |
| `RedisKey` `EnvVar` `ConfigKey` `FeatureToggle` | extractors + k8s |
| `GraphQLOperation` | extractors (planned) |
| `Cluster` `Namespace` `Deployment` `Service` `Ingress` | k8s_sync |
| `ConfigMap` `Secret` (keys only) `CronJob` `PodStatus` | k8s_sync |
| `DockerImage` `ArgoApp` `CiPipeline` | gitops parse (planned) |
| `Memory` | claude-memory md |
| `Ticket` | ticket_client (lazy) |

### Edges

| Edge | Meaning |
|---|---|
| `CONTAINS` `DEFINES` `EXTENDS` `IMPLEMENTS` `USES` `IMPORTS` `ANNOTATED` | code structure |
| `CALLS` `PARAM_TYPE` `RETURNS` `THROWS` `REFERENCES` | call graph |
| `EXPOSES` `CALLS_EXTERNAL` | HTTP |
| `READS` `WRITES` `DELETES` | Mongo |
| `PUBLISH` `SUBSCRIBE` `PRODUCES` `CONSUMES` | NATS / Kafka |
| `LOCKS` `READS_CACHE` `WRITES_CACHE` | Redis |
| `RENDERS` `USES_HOOK` `READS_CONTEXT` | React |
| `TESTS` | Tests → target |
| `READS_ENV` `READS_CONFIG` | env + config |
| `FLOWS_TO` `DATA_FLOWS_TO` | **cross-repo** producer → consumer |
| `HAS_NS` `HAS_WORKLOAD` `TARGETS` `ROUTES` `MOUNTS` `RUNS_IMAGE` `HAS_STATUS` | k8s |
| `IS_SERVICE` `EMITS_IMAGE` `DEPENDS_ON` | repo ↔ runtime |
| `DESCRIBES` | Memory → code target |

`FLOWS_TO` is the key cross-repo edge: producer method in repo A → consumer
method in repo B, joined via NATS subject / Mongo collection / REST path.

## What "full context" means

Given any ticket, `ticket_brief` MCP tool returns a single JSON pack:

- **candidate services** — ranked list of repos touched
- **primary symbols** — top methods with fqn, signature, file:line
- **impact** — upstream callers, data readers, subscribers, tests, deployments
- **build_info** — install + test + package commands for each candidate repo
- **kube** — current deployed image tag, pod phase, restart count (qa + prod)
- **related memories** — claude-memory entries describing touched code
- **integrations touched** — Mongo collections / NATS subjects / REST paths

All within a ~12k token context budget; Qwen has the other 244k for reasoning.

## MCP tools

| Tool | Purpose |
|---|---|
| `sym_lookup` | Hybrid BM25 + vector + rerank search |
| `list_repos` / `list_services` / `list_endpoints` / `list_integrations` | Discovery |
| `graph_neighborhood` | 1-hop in/out neighbors |
| `caller_chain` / `callee_chain` | Up/downstream call traversal |
| `read_source` | Exact file slice |
| `impact` | Blast radius for a change |
| `cross_repo_flow` | Trace producer ↔ consumer via NATS/Mongo/REST |
| `data_lineage` | Writers + readers + deleters of a collection |
| `build_plan` / `test_plan` / `run_commands` | Build / test / run ordered cmds |
| `kube_status` / `kube_describe` / `kube_image_tag` / `kube_config` | k8s read |
| `kube_port_forward_cmd` | Emit port-forward cmd (user runs it) |
| `kube_rollout_restart` | **Write**, requires `confirm:true` |
| `find_doc` / `related_memories` | Memory search |
| `ticket_fetch` / `ticket_brief` | Ticket context pack |

## Directory layout

```
graph_rag/
├── config/                 service-map, ticket-url, link-patterns
├── requirements.txt        Python deps
├── docker-compose.yml      neo4j + bge-m3 + bge-reranker
│
├── javaparser/             Maven shaded jar (existing v3, extended)
├── tsparser/               ts-morph domain layer (React/express/fetch)
├── pyparser/               libcst + ast domain layer (FastAPI/Flask)
│
├── scip_to_neo4j.py        SCIP protobuf → Cypher
├── ingest_jsonl.py         JavaParser/tsparser/pyparser jsonl → Neo4j
├── ingest_memory.py        .md → (:Memory), sha-diff incremental
├── ingest_k8s.py           k8s_sync output → graph
├── ingest_repo_meta.py     per-repo build meta → (:Repo)
│
├── link_services.py        Repo ↔ Deployment
├── link_integrations.py    Cross-repo NATS/Mongo/REST/Kafka FLOWS_TO
├── link_memories.py        Memory → code DESCRIBES via regex
│
├── k8s_sync.py             READ-ONLY cluster snapshot (secrets = keys only)
├── repo_meta.py            build/test/deploy metadata emitter
├── ticket_client.py        Custom-URL ticket fetcher
├── impact.py               Blast-radius CLI
├── ticket_brief.py         All-in-one context pack
├── embed_nodes.py          bge-m3 1024d embedding into Neo4j vector index
├── query.py / semantic.py  Ad-hoc CLI queries
│
├── mcp_server/             MCP stdio (18 tools across 7 modules)
├── bin/                    graph_full_reindex / _incremental / _sanity / memory_sync
└── schedulers/             launchd (Mac) + systemd (NUC) + git post-merge hook
```

## Run

### One-time setup

```bash
# 1. Stack
docker compose up -d

# 2. Python env
python3 -m venv ~/aiforge-venv
~/aiforge-venv/bin/pip install -r requirements.txt
~/aiforge-venv/bin/pip install -e pyparser -e mcp_server

# 3. TS build
cd tsparser && npm ci && npm run build && cd ..

# 4. Java extractor
cd javaparser && mvn -q package -DskipTests && cd ..

# 5. SCIP CLIs (optional; driver skips if absent)
cs install sourcegraph/scip-java
npm i -g @sourcegraph/scip-typescript
pip install scip-python

# 6. Ticket provider
cp config/ticket-url-template.yaml config/ticket-url-template.yaml.local
# edit local copy with real URL + set TICKET_API_TOKEN env var

# 7. Memory repo on GitHub
mkdir -p ~/.claude/memory-repo && cd ~/.claude/memory-repo
git init && git remote add origin git@github.com:<you>/claude-memories.git
bash ~/Documents/codeRepo/AIForgeCrew/scripts/graph_rag/bin/memory_sync.sh

# 8. Kubeconfigs
export QA_KUBECONFIG=~/.kubeconfigs/qa.yaml
export PROD_KUBECONFIG=~/.kubeconfigs/prod.yaml

# 9. Hooks in every repo
for r in ~/Documents/codeRepo/*; do
  [ -d "$r/.git" ] || continue
  cp schedulers/git-hooks/post-merge-index.sh "$r/.git/hooks/post-merge"
  chmod +x "$r/.git/hooks/post-merge"
done
```

### Full reindex

```bash
bash bin/graph_full_reindex.sh          # nuke + rebuild everything
NO_RESET=1 bash bin/graph_full_reindex.sh   # merge mode, keep existing
bash bin/graph_sanity.sh                # counts + 5 acceptance queries
```

Phase 0 nuke drops all nodes + relationships + vector indexes + constraints,
then phases 1-11 rebuild. First v5 run: ~30–40 min on NUC.

### Incremental (automatic)

```bash
# Installed post-merge hook runs on every git pull:
bash bin/graph_incremental.sh <repo_path> <changed_file_1> [...]
```

### Wire MCP server into Qwen / Claude

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`
(Claude Desktop) or your Claude Code `mcp.json`:

```json
{
  "mcpServers": {
    "aiforge-graph": {
      "command": "python",
      "args": ["-m", "aiforge_graph_mcp.server"],
      "env": {
        "NEO4J_URI": "bolt://192.168.70.191:7687",
        "NEO4J_USER": "neo4j",
        "NEO4J_PASS": "password",
        "EMBED_URL": "http://127.0.0.1:8764",
        "RERANK_URL": "http://127.0.0.1:8765",
        "QA_KUBECONFIG": "~/.kubeconfigs/qa.yaml",
        "PROD_KUBECONFIG": "~/.kubeconfigs/prod.yaml",
        "TICKET_API_TOKEN": "${TICKET_API_TOKEN}"
      }
    }
  }
}
```

### Background automation

```bash
# Mac laptop (push memory to GitHub every 30 min)
launchctl bootstrap gui/$(id -u) schedulers/com.aiforge.memory-push.plist

# NUC (pull memory + code repos every 5 min, auto-reindex via post-merge)
sudo cp schedulers/aiforge-*.{service,timer} /etc/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now aiforge-memory-pull.timer aiforge-repo-pull.timer

# Mac (poll k8s state every 15 min)
launchctl bootstrap gui/$(id -u) schedulers/com.aiforge.k8s-sync.plist
```

## Safety

- **Neo4j read-only by default** — all ingest scripts use MERGE; no destructive
  writes unless `graph_full_reindex.sh` runs phase 0
- **k8s tools read-only by default** — `kube_rollout_restart` requires
  `confirm:true` arg; secrets never leave cluster (keys only)
- **Ticket credentials via env var only** — never stored in graph
- **SSH keys untouched** — scripts only read kubeconfig paths

## Extending

1. **New language** — add `foo_parser/` emitting the same jsonl schema
   (`lang`, `repo`, `file`, `classes[]`, `functions[]`, `endpoints[]`,
   `integrations`, `tests[]`); `ingest_jsonl.py` already tolerates superset
   keys.
2. **New integration kind** — add extractor regex, add Cypher pass in
   `link_integrations.py`.
3. **New MCP tool** — drop a module under `mcp_server/aiforge_graph_mcp/tools/`
   exposing `TOOLS` list + `HANDLERS` dict; `server.py` auto-registers.

## Test queries (acceptance)

After reindex these should all return precise `file:line` answers under 2k
tokens:

1. `cross_repo_flow({value:"business.push.request"})`
   → Java publisher `PosServerBackendService.publishToRemoteServer`
   → Java consumer `ClientSyncPushRequestConsumer.onMessage`
2. `sym_lookup({query:"restaurant gate quantity bug"})`
   → Java methods in restaurant flow
3. `caller_chain({key:"...TransactionSyncRulesServiceImpl.applyRulesForBusiness"})`
   → all invokers
4. `sym_lookup({query:"bank statement OCR parse"})`
   → Python `PosPythonBackend` funcs + Java REST callers via
     `CALLS_ENDPOINT`
5. `ticket_brief({id:"ONE-57"})`
   → full context pack < 12k tokens

## History

- **v3** (2026-04-23): Java-only (PosClientBackend), JavaParser + Neo4j +
  nomic-embed-text, 4085 methods / 1362 classes / 8503 CALLS.
- **v5** (2026-04-24): Multi-language (Java/TS/Py) + k8s + memories +
  cross-repo flows + MCP server, switched to bge-m3 + bge-reranker.
