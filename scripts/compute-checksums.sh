#!/usr/bin/env bash
# scripts/compute-checksums.sh — compute sha256 for any manifest entry where
# sha256: "tbd" (or blank), and write it back to security/model-checksums.yml.
# Run after scripts/download-models.sh on first install.
#
# Idempotent: existing non-"tbd" sha256 values are left untouched.
set -euo pipefail

MANIFEST="${MANIFEST:-security/model-checksums.yml}"
[[ -f "$MANIFEST" ]] || { echo "Manifest missing: $MANIFEST" >&2; exit 1; }

UV="${UV:-$HOME/.local/bin/uv}"; command -v "$UV" >/dev/null || UV=uv
"$UV" run --quiet --with pyyaml python - "$MANIFEST" <<'PY'
import hashlib, sys
from pathlib import Path
import yaml

mp = Path(sys.argv[1])
text = mp.read_text()
doc = yaml.safe_load(text) or {}

def sha_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

def sha_dir_mlx(path: Path) -> str:
    shards = sorted(path.rglob("*.safetensors"))
    if not shards:
        raise FileNotFoundError(f"no *.safetensors under {path}")
    lines = []
    for shard in shards:
        rel = shard.relative_to(path)
        lines.append(f"{sha_file(shard)}  ./{rel}\n")
    lines.sort()
    return hashlib.sha256("".join(lines).encode()).hexdigest()

updated = 0
for m in doc.get("models") or []:
    want = str(m.get("sha256", "")).strip().lower()
    if want and want != "tbd":
        continue
    path = Path(m["path"]).expanduser()
    kind = m.get("kind", "file")
    try:
        if kind == "directory":
            if not path.is_dir(): raise FileNotFoundError(path)
            got = sha_dir_mlx(path)
        else:
            if not path.is_file(): raise FileNotFoundError(path)
            got = sha_file(path)
    except Exception as e:
        print(f"  [skip] {m['name']}: {e}")
        continue

    # Rewrite the manifest text in place to preserve formatting + comments.
    # Match the sha256 line for this entry (scoped by name).
    name = m["name"]
    import re
    pat = re.compile(
        rf'(- name:\s*"{re.escape(name)}".*?sha256:\s*)"[^"]*"',
        re.DOTALL,
    )
    new_text, n = pat.subn(lambda mt: f'{mt.group(1)}"{got}"', text, count=1)
    if n:
        text = new_text
        updated += 1
        print(f"  [write] {name} sha256={got[:12]}…")
    else:
        print(f"  [WARN] could not locate {name} sha256 line to rewrite")

if updated:
    mp.write_text(text)
    print(f"\nWrote {updated} sha256 values to {mp}.")
else:
    print("\nNo TBD entries found — nothing to update.")
PY
