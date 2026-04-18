#!/usr/bin/env bash
# scripts/download-models.sh — idempotent download driven by security/model-checksums.yml.
# Per entry: if path exists + checksum matches, skip. Else `lms get -y --mlx <source_url>`.
# Safe to re-run; lms resumes partial downloads.
#
# Requires: LM Studio CLI (`lms`) at ~/.lmstudio/bin/lms.
# Works both on local Mac Studio and via `ssh <host> 'bash -s' < scripts/download-models.sh`.
set -euo pipefail

MANIFEST="${MANIFEST:-security/model-checksums.yml}"
LMS="${LMS:-$HOME/.lmstudio/bin/lms}"
LOG_DIR="${LOG_DIR:-$HOME/aiforge-logs}"
mkdir -p "$LOG_DIR"

if [[ ! -x "$LMS" ]]; then
  echo "LM Studio CLI missing at $LMS. Install LM Studio first." >&2
  exit 1
fi

[[ -f "$MANIFEST" ]] || { echo "Manifest missing: $MANIFEST" >&2; exit 1; }

# Parse YAML via uv-managed Python (no Xcode required).
UV="${UV:-$HOME/.local/bin/uv}"; command -v "$UV" >/dev/null || UV=uv
"$UV" run --quiet --with pyyaml python - "$MANIFEST" "$LMS" "$LOG_DIR" <<'PY' | tee -a "$LOG_DIR/download.log"
import os
import subprocess
import sys
from pathlib import Path

import yaml

manifest, lms, log_dir = sys.argv[1], sys.argv[2], Path(sys.argv[3])
models = yaml.safe_load(Path(manifest).read_text()).get("models") or []

for m in models:
    name = m["name"]
    url = m.get("source_url", "")
    path = Path(m["path"]).expanduser()

    if url == "bundled-with-lm-studio":
        if path.is_file():
            print(f"[skip] {name} (bundled, already present)")
        else:
            print(f"[WARN] {name} expected at {path} but missing — reinstall LM Studio.")
        continue

    if m.get("kind") == "directory" and path.is_dir() and any(path.rglob("*.safetensors")):
        print(f"[skip] {name} (already downloaded)")
        continue
    if m.get("kind") == "file" and path.is_file():
        print(f"[skip] {name} (already downloaded)")
        continue

    if not url:
        print(f"[WARN] {name} has no source_url and no local file — skipping.")
        continue

    print(f"[download] {name} ← {url}")
    # lms resumes partial; -y auto-approve; --mlx restrict format.
    subprocess.run([lms, "get", "-y", "--mlx", url], check=True)

print("\nDownload pass complete. Run verify-checksums.sh next.")
PY
