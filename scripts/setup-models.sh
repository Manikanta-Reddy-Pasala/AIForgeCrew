#!/usr/bin/env bash
# scripts/setup-models.sh — download models listed in security/model-checksums.yml.
# Pre-P0: manifest empty → script exits with "nothing to do".
set -euo pipefail

DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help) echo "Usage: setup-models.sh [--dry-run]"; exit 0 ;;
    *) echo "Unknown arg: $arg" >&2; exit 2 ;;
  esac
done

MANIFEST="security/model-checksums.yml"

python3 - "$MANIFEST" "$DRY_RUN" <<'PY'
import sys
from pathlib import Path

import yaml

manifest = yaml.safe_load(Path(sys.argv[1]).read_text()) or {}
dry = sys.argv[2] == "1"
models = manifest.get("models") or []

if not models:
    print("nothing to do — manifest empty")
    sys.exit(0)

for m in models:
    if dry:
        print(f"would download {m['name']} → {m['path']}")
        continue
    # Actual download plumbed in P0 with huggingface-cli or curl + resume.
    print(f"TODO(P0): download {m['name']}")
PY
