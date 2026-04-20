---
name: aiforge-search
description: Unified memory + knowledge search. Queries three sources in parallel — Hindsight bank (aiforge) for curated agent canon, ChromaDB RAG (.aiforge/rag) for repo code/docs, and Claude memory directories (~/.claude/memory + ~/.claude/projects/*/memory) for cross-session human notes. Use FIRST when researching any topic; cite sources in follow-up comments.
version: 1.0.0
platforms: [macos]
---

# aiforge-search

One query → three sources → unified hits. Run before code changes, design decisions, or ticket responses so the agent has the widest possible context.

## Usage

```bash
{{AIFORGE_PY}} - <<'PY'
import subprocess, json, sys, os, pathlib, textwrap

QUERY = "atomic update pattern mongo"   # <-- replace with your query
TOP_K = 5

def hit(src, body):
    print(f"\n── [{src}] ──\n{textwrap.shorten(body.strip(), 600, placeholder=' …')}")

# 1) Hindsight bank (semantic, agent canon + curated lessons)
try:
    from hindsight_client import Hindsight
    import json as j
    cfg = j.loads(pathlib.Path.home().joinpath(".hermes/hindsight/config.json").read_text())
    from hindsight_embed.daemon_embed_manager import DaemonEmbedManager
    mgr = DaemonEmbedManager()
    if not mgr.is_running("hermes"):
        mgr.ensure_running(cfg, "hermes")
    client = Hindsight(base_url=mgr.get_url("hermes"))
    rr = client.recall(bank_id="aiforge", query=QUERY, max_tokens=800)
    for r in (getattr(rr, "results", []) or [])[:TOP_K]:
        hit(f"hindsight:{getattr(r,'type','?')}", getattr(r, "text", ""))
except Exception as e:
    print(f"hindsight skip: {e}", file=sys.stderr)

# 2) ChromaDB RAG (repo docs + code)
try:
    from aiforge_core.rag import RagIndex
    for h in RagIndex(pathlib.Path(os.path.expanduser("~/AIForgeCrew"))).query(QUERY, top_k=TOP_K):
        hit(f"rag:{h.source}", h.text[:600])
except Exception as e:
    print(f"rag skip: {e}", file=sys.stderr)

# 3) Claude memory directories (cross-session human notes)
grep_paths = []
for p in (pathlib.Path.home()/".claude/memory", pathlib.Path.home()/".claude/projects"):
    if p.is_dir(): grep_paths.append(str(p))
if grep_paths:
    try:
        proc = subprocess.run(
            ["grep", "-rln", "--include=*.md", QUERY, *grep_paths],
            capture_output=True, text=True, timeout=10,
        )
        for ln in proc.stdout.strip().splitlines()[:TOP_K]:
            try:
                excerpt = pathlib.Path(ln).read_text(errors="replace")[:600]
            except Exception: excerpt = ""
            hit(f"claude-md:{ln}", excerpt)
    except Exception as e:
        print(f"claude-md skip: {e}", file=sys.stderr)
PY
```

## When to use

- **Before coding**: hits `hindsight` for prior agent-decisions + `rag` for existing helpers + `claude-md` for human SOP notes.
- **Before reviewing**: hits DESIGN hits from RAG + past incidents in claude-md.
- **Before debugging**: hits prior-bug retros in hindsight + incident logs in claude-md.

## Sources unified

| Source | Location | Populated by |
|---|---|---|
| Hindsight aiforge bank | pg0 @ `~/.pg0/instances/hindsight-embed-hermes` | `scripts/hermes-seed-memory.sh` + session retains |
| ChromaDB RAG | `.aiforge/rag/` | `make rag-reindex` (Java method-chunked + markdown windows) |
| Claude memory MD | `~/.claude/memory/**`, `~/.claude/projects/*/memory/**` | Human-written persona + project notes |

Cite the source path (hindsight | rag:<file> | claude-md:<file>) when you reuse a hit — keeps audit trail provable.
