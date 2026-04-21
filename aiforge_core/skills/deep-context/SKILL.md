---
name: aiforge-deep-context
description: Unified retrieval across the live T4 memory store (42 repos, 20k+ chunks, bge-m3 + pgvector), the claude-memory wing (CLAUDE.md / AGENTS.md / memory/*.md), and per-repo graphify graphs. Given a natural-language query, returns (1) candidate services ranked by evidence, (2) file:line code excerpts per top service, (3) graphify `query` / `explain` output on the top service graphs, (4) claude-memory MD hits. Use FIRST — before touching files, before writing a comment, before deciding which service a ticket is about. Cross-verify every claim against a returned hit.
version: 1.0.0
platforms: [macos]
---

# aiforge-deep-context

**One query → four layers → ranked evidence.**

This is the authoritative retrieval skill for every AIForgeCrew agent (Architect, Sr Developer, Developer, Fact Extract). It hits the *new* T4 Postgres store built with bge-m3 + pgvector that `aiforge memory reindex-code` and `aiforge memory index-claude-memory` populate. The older `aiforge-search` / `aiforge-rag` skills only hit ChromaDB + Hindsight and miss ~20k chunks.

Every agent MUST run this before drawing conclusions about which service a question touches, which files to read, or what prior context exists.

## Timeouts (wall-clock budgets per sub-call)

Every external call inside this skill has a timeout. If a sub-call exceeds its budget, it is dropped (section becomes "(timeout)") and the rest of the skill still returns. No sub-call can hang the agent.

| Sub-call | Budget | Source |
|---|---|---|
| Whole skill (outer) | 120 s | `timeout 120 …` wraps the Python block |
| bge-m3 embed (`:8764/embed`) | 20 s | `urllib` timeout in `aiforge_core.embed._post` |
| Postgres retrieval (T1–T4) | 15 s | `statement_timeout=15s` + connect timeout |
| bge-reranker (`:8765/rerank`) | 20 s | `urllib` timeout in `aiforge_core.retrieval.rerank_http` |
| `graphify query` per repo | 30 s | `subprocess.run(timeout=30)` below |
| file reads (graph.json probe) | OS-level only | `Path.exists()` / local stat |

Individual timeouts are short on purpose: the whole skill must return in ≤ 2 min. If anything blocks, the agent gets partial output and can proceed, rather than hanging the whole session and leaking the flock.

## Usage

```bash
QUERY="${QUERY:-mongoEventListner change stream how it works}"
ROLE="${ROLE:-sr_developer}"       # architect | sr_developer | developer | fact_extract
TOP_K="${TOP_K:-20}"
SKILL_BUDGET="${SKILL_BUDGET:-120}"

# Outer timeout: kills the whole Python block if anything hangs.
# Uses gtimeout (coreutils) on macOS; falls back to `timeout` on Linux.
TIMEOUT_BIN="$(command -v gtimeout || command -v timeout)"

"$TIMEOUT_BIN" --kill-after=10s "${SKILL_BUDGET}s" {{AIFORGE_PY}} - <<PY || {
  rc=$?
  if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
    echo "aiforge-deep-context: TIMEOUT after ${SKILL_BUDGET}s — returning partial / empty context."
    echo "Agent: fall back to 'refine query + re-run' per system prompt fallback chain."
  fi
  exit 0
}
from aiforge_core.store_v2 import Store
from aiforge_core.retrieval import retrieve_for_role
from collections import defaultdict
from pathlib import Path
import subprocess

role  = "$ROLE"
query = r"""$QUERY"""

# --- retrieve with error isolation; Store + retrieve_for_role already timeout via urllib ---
try:
    store = Store(); store.ensure_schema()
    hits  = retrieve_for_role(store, role, query, parent_id=None)
except Exception as e:
    print(f"=== RETRIEVAL ERROR ===\n  {type(e).__name__}: {e}")
    hits = []

# ---- 1. candidate services ----
by_repo = defaultdict(list)
for h in hits:
    repo = (h.metadata or {}).get("repo", "?")
    by_repo[repo].append(h)

print("=== CANDIDATE SERVICES (ranked by evidence) ===")
ranked_repos = sorted(by_repo, key=lambda r: -sum(h.score for h in by_repo[r]))
if not ranked_repos:
    print("  (no hits — retrieval timeout or empty result)")
for repo in ranked_repos[:8]:
    chunks = by_repo[repo]
    agg = sum(h.score for h in chunks)
    print(f"  {repo:<32} score={agg:7.3f}  chunks={len(chunks)}")
print()

# ---- 2. top code chunks ----
print("=== CODE CHUNKS (file:line + excerpt, ≤3 per repo) ===")
for repo in ranked_repos[:5]:
    for h in by_repo[repo][:3]:
        meta = h.metadata or {}
        path = meta.get("path", "?")
        sym  = meta.get("symbol") or ""
        excerpt = (h.text or "").replace("\n", " ").strip()[:260]
        print(f"  [{repo}] {path} {sym}")
        print(f"    {excerpt}")
    print()

# ---- 3. graphify graph context ----
print("=== GRAPHIFY GRAPH CONTEXT (per top service) ===")
graphify_bin = "/Users/manikanta/.local/bin/graphify"
for repo in ranked_repos[:3]:
    if repo == "?": continue
    candidates = [
        Path.home() / "codeRepo" / repo / "graphify-out" / "graph.json",
        Path.home() / "AIForgeCrew" / "graphify-out" / "graph.json" if repo in ("aiforge", "AIForgeCrew") else None,
    ]
    gpath = next((p for p in candidates if p and p.exists()), None)
    if not gpath:
        print(f"  -- {repo}: no graph.json --"); continue
    print(f"  -- {repo} --")
    try:
        out = subprocess.check_output(
            [graphify_bin, "query", query, "--budget", "600", "--graph", str(gpath)],
            text=True, stderr=subprocess.DEVNULL, timeout=30)
        for line in out.splitlines()[:40]:
            print(f"    {line}")
    except subprocess.TimeoutExpired:
        print(f"    (graphify timeout after 30s — skipping this repo)")
    except Exception as e:
        print(f"    (graphify failed: {e})")
    print()

# ---- 4. claude-memory / MD notes ----
print("=== CLAUDE-MEMORY / MD NOTES (human SOP, design notes) ===")
cm_hits = [h for h in hits if (h.metadata or {}).get("repo") == "claude-memory"]
if not cm_hits:
    print("  (no claude-memory hits for this query)")
for h in cm_hits[:6]:
    path = (h.metadata or {}).get("path", "?")
    excerpt = (h.text or "").replace("\n", " ").strip()[:260]
    print(f"  [claude-memory] {path}")
    print(f"    {excerpt}")
PY
```

## Output contract

Every agent run receives four sections in this order:
1. **CANDIDATE SERVICES** — ranked list of repos where the query hits. First entry is the most likely service the ticket is about. Agents MUST cite a service from this list, never guess.
2. **CODE CHUNKS** — top evidence per candidate service, with absolute `file:line` paths. Agents MUST cite these paths in any claim about the code.
3. **GRAPHIFY GRAPH CONTEXT** — AST/community context from per-repo `graphify-out/graph.json` (built by `bulk-index-all-repos.sh`). Agents use this to understand symbol-level call structure and community boundaries without reading raw files.
4. **CLAUDE-MEMORY / MD NOTES** — hits from the `code/claude-memory` wing (CLAUDE.md, AGENTS.md, GEMINI.md, memory/**/*.md). Human-authored SOPs and design notes.

## Cross-verification rule

Every factual claim in a ticket comment or child-ticket description MUST be backed by one of:
- A `file:line` anchor from CODE CHUNKS.
- A graph node mentioned in GRAPHIFY GRAPH CONTEXT.
- A path citation from CLAUDE-MEMORY.

Claims without a returned-hit citation are speculation and must be labelled `(speculative)`. Never repeat what the ticket description already says without adding anchor evidence.

## Role-specific behavior

`retrieve_for_role` tunes the tier mix per role:
- `architect`: T2 (rules) + T4 (code, small) + T3 (skills) → rerank to 10. Cheap brief.
- `sr_developer`: T2 + T3 + T4 (code, wide) + T1 (tickets) → rerank to 12. For deep analysis.
- `developer`: T4 (code, widest, 20) + T3 (skills) + T1 → rerank to 15. For impl work.
- `fact_extract`: T1 only, 200 wide, scoped to `parent_id` when set → rerank to 50.

## Infrastructure

| Component | Location | Role |
|---|---|---|
| T4 code chunks | Postgres `aiforge` DB, table `memories`, wing `code/<repo>` | bge-m3 embeddings, pgvector cosine |
| Claude memory | same DB, wing `code/claude-memory` | 1 chunk per md file, capped at 50 KB |
| bge-m3 embed sidecar | `http://127.0.0.1:8764` | dim=1024, dense only |
| bge-reranker-v2-m3 | `http://127.0.0.1:8765` | FP16 FlagReranker |
| Graphify graphs | `~/codeRepo/<repo>/graphify-out/graph.json` | AST + communities |

## When NOT to use

- Never during the `fact_extract` output — it's supposed to summarize the ticket trace, not re-search.
- Never inside a tight loop — one call per ticket phase, not per file.
