# Stack (what + why)

## Major tools / libraries

### LLM + retrieval

| Tool | What for |
|---|---|
| **LM Studio** (`:1234`) | Local LLM server, OpenAI-compat. Hosts `qwen3.6-27b` + `qwen3.6-35b-a3b@8bit` |
| **bge-m3** (`:8764`) | 1024-d embeddings (ONNX, Mac native) |
| **smolagents** | `ToolCallingAgent` (Doer) + `CodeAgent` (Planner) — the LLM's tool loop |
| **LiteLLM** | Uniform client to LM Studio from smolagents |
| **LangGraph** | State machine that routes a ticket through agents |
| **LangGraph PostgresSaver** | Checkpoints so an interrupted tick can resume |
| **LlamaIndex** | pgvector retriever on the `memories` table |

### Databases

| Tool | What for |
|---|---|
| **Neo4j 5 + APOC + genai** | One DB does three jobs: graph, vector, BM25 (rerank skipped — RRF order is prod ranking) |
| **PostgreSQL 16 + pgvector + pg_trgm + pgcrypto** | Tickets, events, memories, langgraph checkpoints |

### Code parsers (→ graph)

| Tool | What for |
|---|---|
| **JavaParser** (shaded jar) | Java AST: classes, methods, calls, Spring annotations |
| **ts-morph** | TypeScript/React AST: components, hooks, fetch URLs |
| **libcst + ast** | Python: FastAPI/Flask routes, pymongo, env reads |
| **SCIP indexers** | Cross-language symbol + ref index |
| **tree-sitter-java** | Older fast-path parser (kept for incremental) |

### Runtime + plumbing

| Tool | What for |
|---|---|
| **FastAPI / uvicorn** | `http://NUC:8799` REST: tickets, events, health |
| **psycopg v3** | Postgres client (with DSN) |
| **pymongo** | Mongo client (for the `mongo_agent` tool) |
| **httpx** | HTTP client (embed, LM Studio) |
| **MCP stdio** | Protocol for exposing graph-RAG tools to the LLM |
| **Docker / OrbStack** | Container for Neo4j on NUC |
| **systemd --user** | NUC services + timers (`aiforge-*`) |
| **launchd** | Mac Studio services (`com.aiforge.*`) |
| **ssh -L tunnels** | pg-tunnel (MS ←→ NUC pg), lm-tunnel (NUC ←→ MS LLM) |
| **ufw / nmcli** | NUC firewall + static IP |
| **networksetup** | Mac Studio static IP on direct-LAN NIC |
| **gh CLI** | PRs + GitHub API (repo pulls, PR create) |

### Dev

| Tool | What for |
|---|---|
| **uv** | Python venv + deps |
| **pytest + ruff** | Tests + lint |
| **Maven + OpenJDK 21** | Build the JavaParser jar on NUC |
| **mvn** (on MS) | What the Doer runs inside the ticket worktree for `run_compile` |

## Why two machines

One load profile per host.

| Host | Load | Why own machine |
|---|---|---|
| **Mac Studio** (96 GB unified, M3 Ultra) | LLM weights + KV cache + mvn compile | LLM inference pins huge RAM; Metal GPU is Apple-silicon only; Java builds need throughput |
| **NUC 11** (30 GB, i7) | Postgres + Neo4j + FastAPI + indexers + git pulls | Always-on 24/7; cheap RAM for DBs; restarts don't interrupt LLM serving |
| Laptop | Dev shell, queries | Not part of runtime |

Splitting them means:
- Bouncing the API doesn't unload a 38 GB model
- Nothing else competes with the LLM for RAM (avoids the 512K-ctx OOM we hit earlier)
- NUC can reindex code overnight without touching LLM latency
- Either host can reboot alone; only cross-host glue is two ssh tunnels
