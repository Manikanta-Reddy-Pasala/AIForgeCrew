#!/usr/bin/env bash
# scripts/delete-unused-models.sh — delete MLX model dirs from ~/.lmstudio/models/
# that are NOT listed in security/model-checksums.yml.
#
# Safe: only touches dirs under ~/.lmstudio/models/<publisher>/. Preserves
# anything referenced by the manifest. Runs `lms unload <model>` first if
# the model is currently resident.
#
# Idempotent. Dry-run by default; set CONFIRM=1 to actually delete.
set -euo pipefail

[[ "$(uname -s)" == "Darwin" ]] || { echo "delete-unused-models: macOS only" >&2; exit 1; }

MANIFEST="${MANIFEST:-security/model-checksums.yml}"
MODELS_ROOT="${MODELS_ROOT:-$HOME/.lmstudio/models}"
LMS="${LMS:-$HOME/.lmstudio/bin/lms}"

[[ -f "$MANIFEST" ]] || { echo "Manifest missing: $MANIFEST" >&2; exit 1; }
[[ -d "$MODELS_ROOT" ]] || { echo "$MODELS_ROOT missing — nothing to clean"; exit 0; }

# Build set of allowed paths from manifest.
UV="${UV:-$HOME/.local/bin/uv}"; command -v "$UV" >/dev/null || UV=uv

ALLOWED=$(
  "$UV" run --quiet --with pyyaml python - "$MANIFEST" "$MODELS_ROOT" <<'PY'
import os, sys
from pathlib import Path
import yaml
doc = yaml.safe_load(Path(sys.argv[1]).read_text()) or {}
root = Path(sys.argv[2]).resolve()
for m in doc.get("models") or []:
    p = Path(m["path"]).expanduser().resolve()
    # Only care about entries under MODELS_ROOT (skip bundled embeds etc).
    try:
        p.relative_to(root)
        print(str(p).rstrip("/"))
    except ValueError:
        continue
PY
)

echo "=== allowed paths from manifest ==="
echo "$ALLOWED"
echo

# Enumerate every directory that holds .safetensors under MODELS_ROOT.
# Structure: ~/.lmstudio/models/<publisher>/<repo>/
declare -a TO_DELETE
while IFS= read -r -d '' dir; do
  canon=$(cd "$dir" && pwd -P | sed 's:/*$::')
  if ! grep -Fxq "$canon" <<< "$ALLOWED"; then
    TO_DELETE+=("$canon")
  fi
done < <(
  find "$MODELS_ROOT" -type d -mindepth 2 -maxdepth 3 -print0 |
    while IFS= read -r -d '' d; do
      if find "$d" -maxdepth 1 -name "*.safetensors" -print -quit 2>/dev/null | grep -q . ; then
        printf '%s\0' "$d"
      fi
    done
)

if (( ${#TO_DELETE[@]} == 0 )); then
  echo "No unused model dirs found."
  exit 0
fi

echo "=== unused model dirs (will delete) ==="
total_gb=0
for d in "${TO_DELETE[@]}"; do
  gb=$(du -sk "$d" 2>/dev/null | awk '{printf "%.1f", $1/1024/1024}')
  echo "  $d  (${gb} GB)"
done

if [[ "${CONFIRM:-0}" != "1" ]]; then
  echo
  echo "DRY RUN — re-run with CONFIRM=1 to actually delete."
  exit 0
fi

# Unload first if LM Studio has them resident.
if [[ -x "$LMS" ]]; then
  echo
  echo "=== unloading any resident models first ==="
  for d in "${TO_DELETE[@]}"; do
    # Derive LM Studio identifier: <publisher>/<repo>
    ident=$(sed "s|$MODELS_ROOT/||" <<< "$d")
    "$LMS" unload "$ident" 2>/dev/null || true
  done
fi

echo
echo "=== deleting ==="
for d in "${TO_DELETE[@]}"; do
  # Safety guard: never rm outside MODELS_ROOT.
  case "$d" in
    "$MODELS_ROOT"/*) rm -rf "$d" && echo "  deleted $d" ;;
    *) echo "  REFUSE (outside $MODELS_ROOT): $d" >&2 ;;
  esac
done

# Clean up empty publisher dirs.
find "$MODELS_ROOT" -mindepth 1 -maxdepth 1 -type d -empty -delete 2>/dev/null || true

echo
echo "Done. Manifest-tracked models remain:"
for line in $ALLOWED; do
  [[ -d "$line" ]] && echo "  ✓ $line" || echo "  ✗ $line (missing — run scripts/download-models.sh)"
done
