# Graphify usage by AIForge agents

Graphify produces a per-repo `graphify-out/` directory containing a community-clustered code graph. Agents consume this in three ways.

## What graphify produces

```
<repo>/graphify-out/
├── GRAPH_REPORT.md     # human-readable: top god-nodes, hot edges, community summaries
├── graph.json          # nodes[] + edges[] + community labels
├── wiki/index.md       # per-community markdown wiki
└── cache/              # incremental cache (not consumed)
```

Refreshed every 6 hours by `aiforge-graphify-all.timer` for all repos in `~/.aiforge/scheduler.yaml`.

## How agents use it

### 1. Architect agent — read GRAPH_REPORT.md directly

Before answering "how does X relate to Y" or "what's the entry point of repo Z", the architect reads `graphify-out/GRAPH_REPORT.md`. CLAUDE.md rule:

> Before answering architecture or codebase questions, read graphify-out/GRAPH_REPORT.md for god nodes and community structure.

### 2. Planner — community-scoped allowed_files

When `_fetch_allowed_files()` runs the hybrid retrieval, it can filter results by graph community membership. Files in the same community as the ticket's keyword anchor rank higher than orphan files. Implemented in `aiforge_core/index/graphify_loader.py` — loads `graph.json` into Neo4j as `:GraphifyNode {id, community, name, repo}` nodes. Currently 1,746 GraphifyNode rows persisted.

Cypher example (community-scoped retrieval):

```cypher
MATCH (g1:GraphifyNode {repo: $repo, name: $anchor})
MATCH (g2:GraphifyNode {repo: $repo, community: g1.community})
RETURN g2.name, g2.path
```

### 3. Doer — edge-weight-guided multi-file edits

Edges in `graph.json` carry a weight. When the doer needs to know "which files do I need to read together", it consults the graph: high-weight neighbors of an edited file are prefetched into context.

## Browsing in the Memory UI

http://192.168.70.115:8767 → Graph tab.

- Lists all repos with graphify-out (size, node count, last refresh)
- Per repo: open GRAPH_REPORT.md inline · open raw graph.json · open wiki tree

## Operator commands

```bash
# Manual refresh for one repo:
cd <repo>
graphify update .

# Manual refresh all scheduled repos:
systemctl --user start aiforge-graphify-all.service

# Watch progress:
tail -f ~/.aiforge/logs/graphify-all.log

# Inspect timer:
systemctl --user list-timers aiforge-graphify-all.timer
```

## Loader contract (Neo4j sync)

`aiforge_core/index/graphify_loader.py::load_repo_graph(repo, path)`:
- Reads `<path>/graphify-out/graph.json`
- Upserts `:GraphifyNode` per node (`MERGE id`, `SET community/name/repo`)
- Upserts `:GRAPH_EDGE` per edge (weight, type)
- Idempotent — safe to re-run after every graphify update

Triggered after each graphify-update batch via post-hook (TODO — currently manual).

## Failure modes

- `graph.json` exceeds 50 MB → `/api/graphify/{repo}/graph` returns 413; download directly via `cat <repo>/graphify-out/graph.json`.
- `wiki/index.md` missing → `has_wiki: false` in `/api/graphify`. Run `graphify update --wiki` to generate.
- LLM-summary mode requires `MOONSHOT_API_KEY` (Kimi K2). AST-only mode is default and is what the timer uses (no LLM cost).
