#!/usr/bin/env bash
# One-shot migration from legacy stores to store_v2.
#   - Wipes chroma-backed rag/ DB
#   - Reindexes all repo sources into T4
#   - Leaves T2/T3 empty (seeded manually via `aiforge propose approve`)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# 1. Ensure pg schema
bash scripts/install-pg-aiforge.sh

# 2. Drop legacy chroma dir
rm -rf .aiforge/rag .aiforge/chroma 2>/dev/null || true

# 3. Reindex T4 via new CLI
python3 -m aiforge_core.cli memory reindex-code --repo aiforge --root "$REPO_ROOT"

echo "migration done. Legacy stores gone. T4 reindexed."
