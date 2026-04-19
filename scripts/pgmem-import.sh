#!/usr/bin/env bash
# scripts/pgmem-import.sh — import codeRepo + Claude memory into pgvector.
# Chunks text (~1200 chars) and bulk-inserts with per-chunk nomic embeddings.
# Runs on Mac Studio.
set -euo pipefail

REPO="${REPO:-$HOME/AIForgeCrew}"
CODE_DIR="${CODE_DIR:-$HOME/codeRepo}"
CLAUDE_MEMORY="${CLAUDE_MEMORY:-$HOME/.claude/memory}"
CLAUDE_OBSERVER="${CLAUDE_OBSERVER:-$HOME/.claude/projects/-Users-manip--claude-mem-observer-sessions}"

export PATH="/opt/homebrew/opt/postgresql@16/bin:$PATH"
export AIFORGE_PGMEM_DSN="${AIFORGE_PGMEM_DSN:-host=127.0.0.1 port=5432 dbname=aiforge}"

# Ensure psycopg is in the venv.
"$REPO/.venv/bin/python" -c "import psycopg" 2>/dev/null || \
  ~/.local/bin/uv pip install --python "$REPO/.venv/bin/python" "psycopg[binary]"

"$REPO/.venv/bin/python" - <<PY
import os, sys, pathlib
sys.path.insert(0, "$REPO")
from aiforge_core.pgmem import PgMemBus

bus = PgMemBus()
bus.ensure_schema()

# Chunker — char-based so we don't load giant spaCy models.
def chunks(text, size=1200, overlap=200):
    if len(text) <= size:
        return [text]
    out, i = [], 0
    while i < len(text):
        out.append(text[i:i+size])
        i += size - overlap
    return out

SKIP_NAMES = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache",
              ".ruff_cache", ".mypy_cache", "dist", "build", "target"}
TEXT_EXT   = {".py", ".md", ".txt", ".yaml", ".yml", ".json", ".sh", ".js",
              ".ts", ".tsx", ".jsx", ".rs", ".go", ".java", ".sql"}

def iter_text_files(root):
    root = pathlib.Path(root)
    if not root.is_dir():
        return
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if any(part in SKIP_NAMES for part in p.parts):
            continue
        if p.suffix.lower() not in TEXT_EXT and p.name not in ("Makefile", "Dockerfile"):
            continue
        try:
            if p.stat().st_size > 500_000:
                continue  # skip huge files
            yield p
        except OSError:
            continue

def mine(root, wing, room="code"):
    batch = []
    total = 0
    for f in iter_text_files(root):
        try:
            txt = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = str(f.relative_to(root))
        for i, chunk in enumerate(chunks(txt)):
            batch.append({
                "wing": wing, "room": room,
                "source": f"{rel}",
                "title": rel.split("/")[-1],
                "text": chunk,
                "metadata": {"chunk": i, "file": rel},
            })
        if len(batch) >= 64:
            n = bus.bulk_insert(batch); total += n
            print(f"  inserted {total} chunks…", flush=True)
            batch = []
    if batch:
        n = bus.bulk_insert(batch); total += n
    print(f"  wing {wing!r}: {total} chunks")

# --- codeRepo: one wing per subrepo ---
code_dir = pathlib.Path("$CODE_DIR")
if code_dir.is_dir():
    for sub in sorted(code_dir.iterdir()):
        if sub.is_dir() and not sub.name.startswith("."):
            print(f"[code] {sub.name}")
            mine(sub, wing=f"repo/{sub.name}", room="code")
else:
    print(f"[skip] {code_dir} missing")

# --- Claude memory ---
cm = pathlib.Path("$CLAUDE_MEMORY")
if cm.is_dir():
    print(f"[claude-daily]")
    mine(cm, wing="claude-daily", room="daily")

obs = pathlib.Path("$CLAUDE_OBSERVER")
if obs.is_dir():
    print(f"[claude-observer]")
    mine(obs, wing="claude-observer", room="session")

print()
print("=== wing_counts ===")
for wing, n in bus.wing_counts().items():
    print(f"  {wing:<40} {n}")
PY
