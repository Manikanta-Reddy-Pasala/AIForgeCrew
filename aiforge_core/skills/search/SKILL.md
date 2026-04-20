---
name: aiforge-search
description: Unified memory + knowledge search. Queries three sources — Hindsight bank (aiforge) for curated agent canon + Claude persona memory, ChromaDB RAG (.aiforge/rag) for repo code/docs, and Claude memory directories for literal grep. Use FIRST when researching any topic; cite sources in follow-up comments.
version: 1.0.0
platforms: [macos]
---

# aiforge-search

One query → three sources → unified hits. Run before code changes, design decisions, or ticket responses so the agent has widest possible context.

## Usage

```bash
QUERY="${QUERY:-atomic update pattern mongo}"
TOP_K="${TOP_K:-5}"

echo "=== QUERY: $QUERY ==="

# 1) Hindsight (via REST API — daemon auto-starts)
echo
echo "-- SOURCE 1: Hindsight aiforge bank --"
BODY=$(python3 -c "import json,sys; print(json.dumps({'query': sys.argv[1], 'max_tokens': 800}))" "$QUERY")
curl -sS -X POST http://127.0.0.1:9177/v1/default/banks/aiforge/memories/recall \
  -H 'Content-Type: application/json' -d "$BODY" 2>/dev/null \
  | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    for r in (d.get('results') or [])[:$TOP_K]:
        t = (r.get('text') or '').strip()
        if len(t) > 220: t = t[:217] + '...'
        print(f\"  [{r.get('type','?')}] {t}\")
except Exception as e:
    print(f'  (skip: {e})')
"

# 2) ChromaDB RAG (aiforge venv)
echo
echo "-- SOURCE 2: RAG (ChromaDB) --"
{{AIFORGE_PY}} - <<PY
from pathlib import Path
from aiforge_core.rag import RagIndex
import os
os.chdir(os.path.expanduser('~/AIForgeCrew'))
for h in RagIndex(Path('.')).query("$QUERY", top_k=$TOP_K):
    t = (h.text or '').replace('\n', ' ')
    if len(t) > 220: t = t[:217] + '...'
    print(f"  [{h.source}] {t}")
PY

# 3) Claude memory md (literal grep)
echo
echo "-- SOURCE 3: Claude memory md --"
found=0
for root in "$HOME/.claude/memory" "$HOME/.claude/projects"; do
  [[ -d "$root" ]] || continue
  while read -r line; do
    [[ -z "$line" ]] && continue
    short=${line/$HOME/~}
    excerpt=$(head -c 200 "$line" | tr '\n' ' ')
    echo "  [$short] $excerpt"
    ((found++))
    [[ $found -ge $TOP_K ]] && break 2
  done < <(grep -irln --include="*.md" "$QUERY" "$root" 2>/dev/null)
done
[[ $found -eq 0 ]] && echo "  (no literal hits — hindsight already covers semantic matches)"
```

## Sources unified

| Source | Location | Populated by |
|---|---|---|
| Hindsight aiforge bank | pg0 @ `~/.pg0/instances/hindsight-embed-hermes`, API @ :9177 | `scripts/hermes-seed-memory.sh` + session retains |
| ChromaDB RAG | `.aiforge/rag/` | `make rag-reindex` (Java method-chunked + markdown windows) |
| Claude memory MD | `~/.claude/memory/**`, `~/.claude/projects/*/memory/**` | Human-written persona + project notes |

## When to use

- **Before coding**: hindsight for prior agent-decisions + RAG for existing helpers + claude-md for human SOP.
- **Before reviewing**: DESIGN hits from RAG + past incidents in claude-md.
- **Before debugging**: prior-bug retros in hindsight + incident logs in claude-md.

Cite source path (hindsight | rag:<file> | claude-md:<file>) when reusing a hit — keeps audit trail provable.
