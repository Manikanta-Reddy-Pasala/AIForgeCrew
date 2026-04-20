#!/usr/bin/env bash
# scripts/verify-checksums.sh — verify every entry in security/model-checksums.yml.
# Handles both single-file (GGUF) and directory (MLX) models.
set -euo pipefail

MANIFEST="${MANIFEST:-security/model-checksums.yml}"
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

UV="${UV:-$HOME/.local/bin/uv}"; command -v "$UV" >/dev/null || UV=uv
"$UV" run --quiet --with pyyaml python - "$MANIFEST" <<'PY'
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


def sha_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha_dir_mlx(path: Path) -> str:
    r"""
    Match: cd DIR && find . -name "*.safetensors" -exec shasum -a 256 {} \; | sort | shasum -a 256
    shasum output format: '<hex>  ./<rel-path>\n'. Sort lexicographically, concat, re-hash.
    """
    shards = sorted(path.rglob("*.safetensors"))
    if not shards:
        raise FileNotFoundError(f"no *.safetensors under {path}")
    lines = []
    for shard in shards:
        rel = shard.relative_to(path)
        lines.append(f"{sha_file(shard)}  ./{rel}\n")
    lines.sort()
    payload = "".join(lines).encode()
    return hashlib.sha256(payload).hexdigest()


failed = []
ok = 0
for m in models:
    name = m["name"]
    kind = m.get("kind", "file")
    raw_path = m["path"]
    path = Path(raw_path).expanduser()
    want = str(m["sha256"]).strip().lower()

    # Allow "tbd" placeholders — new entries pending first compute-checksums.sh.
    if want == "tbd":
        print(f"  ⊙ {name}  (sha256=tbd — run scripts/compute-checksums.sh after download)")
        continue

    try:
        if kind == "file":
            if not path.is_file():
                failed.append(f"{name}: missing file {path}")
                continue
            got = sha_file(path)
        elif kind == "directory":
            if not path.is_dir():
                failed.append(f"{name}: missing dir {path}")
                continue
            got = sha_dir_mlx(path)
        else:
            failed.append(f"{name}: unknown kind '{kind}'")
            continue
    except Exception as e:
        failed.append(f"{name}: error {e}")
        continue

    if got != want:
        failed.append(f"{name}: sha256 mismatch (got {got[:12]}… want {want[:12]}…)")
    else:
        ok += 1
        print(f"  ✓ {name}  ({got[:12]}…)")

if failed:
    print("\nCHECKSUM VERIFICATION FAILED:", file=sys.stderr)
    for f in failed:
        print(f"  - {f}", file=sys.stderr)
    sys.exit(1)

print(f"\nAll {ok} model checksums verified.")
PY
