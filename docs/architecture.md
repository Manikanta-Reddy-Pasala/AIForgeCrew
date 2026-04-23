# Architecture (2 machines, 2026-04-24)

Two hosts, one laptop for dev.

```
Mac Studio 192.168.70.185         NUC 192.168.70.191 (static)
(LLM-only, 96 GB unified)          (storage + services, 30 GB RAM)
─────────────────────────          ──────────────────────────────
LM Studio        :1234             Postgres        :5432   (aiforge db)
  qwen3.6-27b             16 GB    Neo4j (docker)  :7474 / :7687
  qwen3.6-35b-a3b@8bit    38 GB    aiforge-api     :8799   (FastAPI)
  qwen3-coder-next       (MCP)
  nomic-embed-text-v1.5  (768d)
graph-runner (launchd, 60 s)      aiforge-* timers (systemd --user)
caffeinate (wake lock)              ├─ repo-pull          5 min
pg-tunnel (ssh -L 5433→NUC:5432)    ├─ memory-pull        5 min
com.aiforge.k8s-sync (launchd,      ├─ git-pull          10 min
  15m, qa+prod → NUC Neo4j)         ├─ file-indexer      30 min
                                    ├─ post-merge hooks  (graph_rag incremental)
                                    └─ reindex-daily     02:00
                                   lm-tunnel (ssh -L 1235→MS:1234)
                                   ┌─────────────────────────────┐
                                   │ Direct LAN 10.10.10.1 ↔ .2 │
                                   │  ~0.6 ms RTT, 1 GbE         │
                                   └─────────────────────────────┘
```

## Who owns what

| Concern | Host |
|---|---|
| LLM inference (planner, doer, feedback, MCP Qwen) | Mac Studio (LM Studio) |
| Embeddings (nomic-embed-text via LM Studio) | Mac Studio (LM Studio) |
| Orchestrator (graph-runner ticks) | Mac Studio |
| Java repo worktrees + `mvn compile` | Mac Studio |
| Tickets + memories + checkpoints (Postgres) | NUC |
| Code graph + vector index (Neo4j) | NUC |
| REST API `/api/*` | NUC |
| Code indexers (file-indexer, reindex-daily) | NUC |
| Git pulls (`~/codeRepo/*`, `~/.claude/memory`) | NUC |

## Cross-host bridges

Only ssh tunnels — no rsync, no shared FS.

- `com.aiforge.pg-tunnel` (MS): `ssh -L 127.0.0.1:5433:127.0.0.1:5432 mani@10.10.10.2`
  — graph-runner hits postgres via MS-loopback (macOS Sequoia sandboxes LAN `connect()`).
- `lm-tunnel.service` (NUC): `ssh -L 127.0.0.1:1235:127.0.0.1:1234 manikanta@10.10.10.1`
  — NUC scripts reach MS LM Studio via NUC-loopback.

## Data flow

Source of truth: GitHub. All repos + `~/.claude/memory` live in git.
Both hosts `git pull` directly. Zero rsync between them.

```
GitHub ──pull──> NUC:~/codeRepo/*       (every 5 min)
GitHub ──pull──> MS:~/codeRepo/*         (per-ticket worktree, on demand)
laptop ──push──> GitHub (AIForgeCrew)   ──pull──> both hosts (every 10 min)
```
