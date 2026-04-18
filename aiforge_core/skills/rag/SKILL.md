---
name: aiforge-rag
description: Semantic search over the AIForgeCrew project docs, agent configs, DESIGN.md, and memory YAML files. Backed by ChromaDB at .aiforge/rag/. Use BEFORE writing code to find prior art, DESIGN rules, or similar existing components.
version: 1.0.0
platforms: [macos]
---

# aiforge-rag

Local semantic search over README + DESIGN + docs/**, agents/**, security/**, memory/**.

## Query

```bash
{{AIFORGE_PY}} - <<'PY'
from pathlib import Path
from aiforge_core.rag import RagIndex
for h in RagIndex(Path(".")).query("coverage gate enforcement", top_k=5):
    print(f"[{h.source}] {h.text[:300]}")
PY
```

## Reindex

Only the human or a release script runs this. Agents don't trigger it.

```bash
make rag-reindex
```

## When to use

- Before designing: "what does DESIGN say about X?"
- Before writing: "do we already have a helper for Y?"
- Before reviewing: "what ADR covers the area this PR touches?"

## Chunks returned

Each hit has `source` (repo-relative path) + `text` (≤1200 chars). Cite the
source in your ticket comment so the audit trail points to the evidence.
