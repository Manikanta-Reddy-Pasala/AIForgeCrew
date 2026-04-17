#!/usr/bin/env bash
# scripts/verify-checksums.sh — verify every entry in security/model-checksums.yml.
set -euo pipefail

MANIFEST="security/model-checksums.yml"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --manifest) MANIFEST="$2"; shift 2 ;;
    -h|--help) echo "Usage: verify-checksums.sh [--manifest PATH]"; exit 0 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [[ ! -f "$MANIFEST" ]]; then
  echo "Manifest not found: $MANIFEST" >&2
  exit 1
fi

python3 - "$MANIFEST" <<'PY'
import hashlib
import sys
from pathlib import Path

import yaml

manifest_path = Path(sys.argv[1])
doc = yaml.safe_load(manifest_path.read_text()) or {}
models = doc.get("models") or []
if not models:
    print("No models declared — OK.")
    sys.exit(0)

failed = []
for m in models:
    name, path, want = m["name"], Path(m["path"]), m["sha256"]
    if not path.is_file():
        failed.append(f"{name}: missing file {path}")
        continue
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    got = h.hexdigest()
    if got != want:
        failed.append(f"{name}: sha256 mismatch (got {got[:12]}… want {want[:12]}…)")

if failed:
    print("CHECKSUM VERIFICATION FAILED:", file=sys.stderr)
    for f in failed:
        print(f"  - {f}", file=sys.stderr)
    sys.exit(1)

print(f"All {len(models)} model checksums verified.")
PY
