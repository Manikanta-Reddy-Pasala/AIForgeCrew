#!/usr/bin/env bash
# scripts/hermes-seed-memory.sh — seed the Hindsight `aiforge` bank with
# existing Claude memory (~/.claude/memory, ~/.claude/projects/*/memory) and
# AIForgeCrew repo docs.
#
# Starts the Hindsight local_embedded daemon, opens a hindsight-client, and
# calls `retain()` per file with appropriate tags.
#
# Idempotent — Hindsight dedupes by content hash + update_mode.
set -euo pipefail

[[ "$(uname -s)" == "Darwin" ]] || { echo "hermes-seed-memory: macOS only" >&2; exit 1; }

HERMES_DIR="${HERMES_DIR:-$HOME/.hermes}"
HERMES_PY="${HERMES_PY:-$HERMES_DIR/hermes-agent/venv/bin/python}"
CLAUDE_MEMORY="${CLAUDE_MEMORY:-$HOME/.claude/memory}"
CLAUDE_PROJECTS="${CLAUDE_PROJECTS:-$HOME/.claude/projects}"
REPO_DIR="${REPO_DIR:-$HOME/AIForgeCrew}"
BANK_ID="${BANK_ID:-aiforge}"
PROFILE="${PROFILE:-hermes}"

[[ -x "$HERMES_PY" ]] || { echo "hermes venv python missing at $HERMES_PY" >&2; exit 1; }
[[ -f "$HERMES_DIR/hindsight/config.json" ]] || { echo "Hindsight not configured — run scripts/hermes-setup-hindsight.sh" >&2; exit 1; }

# Daemon manager shells out to `uvx hindsight-api@...` — put ~/.local/bin on PATH.
export PATH="$HOME/.local/bin:/opt/homebrew/opt/postgresql@16/bin:$PATH"

# Make sure LM Studio is reachable — Hindsight daemon relies on it for
# fact extraction on retain().
if ! curl -s -m 3 http://localhost:1234/v1/models >/dev/null; then
  echo "WARN: LM Studio not responding at :1234 — daemon retain will stall" >&2
fi

export CLAUDE_MEMORY CLAUDE_PROJECTS REPO_DIR BANK_ID PROFILE

"$HERMES_PY" - <<'PY'
import json, os, hashlib, sys
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_DIR", str(Path.home() / ".hermes")))
CFG = json.loads((HERMES_HOME / "hindsight" / "config.json").read_text())
PROFILE = os.environ.get("PROFILE", "hermes")
BANK = os.environ.get("BANK_ID", "aiforge")

# 1. Start local_embedded daemon if not running.
from hindsight_embed.daemon_embed_manager import DaemonEmbedManager
mgr = DaemonEmbedManager()
if not mgr.is_running(PROFILE):
    print(f">>> starting Hindsight daemon (profile={PROFILE})", flush=True)
    mgr.ensure_running(CFG, PROFILE)
base_url = mgr.get_url(PROFILE)
print(f"daemon URL: {base_url}", flush=True)

# 2. Connect client + ensure bank exists.
from hindsight_client import Hindsight
client = Hindsight(base_url=base_url)
banks = {b.bank_id for b in client.banks.list()} if hasattr(client.banks, "list") else set()
if BANK not in banks:
    try:
        client.create_bank(bank_id=BANK)
        print(f"created bank: {BANK}", flush=True)
    except Exception as e:
        print(f"create_bank warn: {e}", flush=True)

# 3. Walk source trees + retain each file.
def collect():
    # Curated — NO raw session/tool-result files. Only human-written notes
    # + project docs. Each file → one retain() call → one LLM extraction.
    # Session noise in ~/.claude/projects/*/tool-results is skipped (big, no signal).
    files = []
    CM = os.environ.get("CLAUDE_MEMORY", "")
    CP = os.environ.get("CLAUDE_PROJECTS", "")
    REPO = os.environ.get("REPO_DIR", "")

    # 1. Global Claude memory (MEMORY.md + user/feedback/project/reference notes)
    if CM:
        p = Path(CM)
        if p.is_dir():
            for f in p.rglob("*.md"):
                if f.is_file() and f.stat().st_size <= 100_000:
                    files.append((f, "claude-global"))

    # 2. Per-project memory/ dirs only — skip tool-results + session logs.
    if CP:
        p = Path(CP)
        if p.is_dir():
            for mem_dir in p.glob("*/memory"):
                if mem_dir.is_dir():
                    proj_name = mem_dir.parent.name.lstrip("-").replace("-", "/")
                    for f in mem_dir.rglob("*.md"):
                        if f.is_file() and f.stat().st_size <= 100_000:
                            files.append((f, f"claude-proj:{proj_name[:60]}"))

    # 3. AIForgeCrew repo — docs + DESIGN + README only.
    for rel, tag in (
        ("DESIGN.md", "aiforge-design"),
        ("README.md", "aiforge-readme"),
    ):
        p = Path(REPO) / rel
        if p.is_file(): files.append((p, tag))
    docs = Path(REPO) / "docs"
    if docs.is_dir():
        for f in docs.rglob("*.md"):
            if f.is_file() and f.stat().st_size <= 100_000:
                files.append((f, "aiforge-docs"))
    return files

files = collect()
print(f"seeding {len(files)} files...", flush=True)

ok = fail = 0
for f, tag in files:
    try:
        text = f.read_text(encoding="utf-8", errors="replace").strip()
        if not text: continue
        doc_id = hashlib.sha1(f"{tag}:{f}".encode()).hexdigest()
        client.retain(
            bank_id=BANK,
            content=text,
            context=f.name,
            document_id=doc_id,
            tags=["seed", tag],
            update_mode="replace",
            metadata={"source": str(f), "tag": tag},
        )
        ok += 1
        if ok % 10 == 0:
            print(f"  retained {ok}...", flush=True)
    except Exception as e:
        fail += 1
        print(f"  [fail] {f}: {e}", flush=True)

print(f"\n=== seeded: {ok} ok, {fail} fail ===")

# 4. Smoke-test recall.
try:
    rr = client.recall(bank_id=BANK, query="AIForgeCrew DESIGN permission matrix", max_tokens=512)
    results = getattr(rr, "results", []) or []
    print(f"\nrecall probe ({len(results)} results):")
    for r in results[:3]:
        # RecallResult has .text + .context + .type (experience/world). No .content.
        text = getattr(r, "text", "") or ""
        ctx = getattr(r, "context", "") or ""
        print(f"  [{ctx}] {text[:120]}")
except Exception as e:
    print(f"recall probe failed: {e}")
PY
