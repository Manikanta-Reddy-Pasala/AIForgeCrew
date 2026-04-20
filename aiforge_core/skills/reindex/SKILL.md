---
name: aiforge-reindex
description: Rebuild ChromaDB RAG index + re-seed Hindsight memory. Use after docs/agents/security edits or after a major code landing so aiforge-search hits fresh content. NOT run mid-ticket — belongs to release/checkpoint flow.
version: 1.0.0
platforms: [macos]
---

# aiforge-reindex

## Rebuild RAG (method-boundary Java chunker + markdown windows)

```bash
cd ~/AIForgeCrew && .venv/bin/python scripts/rag-reindex-multi.py
```

Indexes: AIForgeCrew docs + PosPythonBackend + TallyConnector + MongoDbService + PosDataSyncService. Java chunked at method boundaries, markdown/etc at 2500-char windows. Stored in `.aiforge/rag/`.

## Re-seed Hindsight memory bank (Claude md + AIForge docs → aiforge bank)

```bash
cd ~/AIForgeCrew && \
CLAUDE_MEMORY=$HOME/.claude/memory \
CLAUDE_PROJECTS=$HOME/.claude/projects \
REPO_DIR=$HOME/AIForgeCrew BANK_ID=aiforge \
bash scripts/hermes-seed-memory.sh
```

NVIDIA NIM extracts facts per file; idempotent by document_id (sha1 of tag+path).

## Verify counts after re-seed

```bash
PGPASSWORD=hindsight ~/.pg0/installation/18.1.0/bin/psql -h 127.0.0.1 -p 5433 -U hindsight -d hindsight -At -c \
"SELECT fact_type, COUNT(*) FROM memory_units WHERE bank_id='aiforge' GROUP BY 1"
```

## When to use

- After merging to main — re-seed so hindsight reflects current canon.
- After doc-heavy PRs (architecture, runbook, troubleshooting) — RAG + hindsight both lag.
- After deleting stale memories — seed repopulates from source of truth.

## When NOT to use

- Mid-ticket. Seeding takes ~2-5 min per source tree; RAG reindex takes ~30-60s per external repo. Don't block a bug-fix run on these.
- Just before dispatch — queue will be cold. Seed runs async via NIM; counts grow for minutes after the shell command returns.
