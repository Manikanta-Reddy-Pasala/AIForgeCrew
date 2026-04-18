#!/usr/bin/env bash
# scripts/load-models.sh — pre-load role models with 128K context into LM Studio.
# Ensures each model is resident with expected context length before agents connect.
#
# Memory math on 96 GB M3 Ultra with q8 KV (halved):
#   Qwen3.6-35B-A3B MoE  4bit weights 20 GB + 128K KV q8 ~5 GB = ~25 GB
#   GLM-4.7-Flash   MoE  6bit weights 24 GB + 128K KV q8 ~5 GB = ~29 GB
#   Gemma-4-31B    dense 4bit weights 18 GB + 128K KV q8 ~12 GB = ~30 GB
#   Total resident: ~84 GB of 96 GB (~12 GB buffer)
# Without KV quantization, Gemma KV alone at 128K ≈ 24 GB → overshoots.
# Note: LM Studio may not accept --kv-cache-quantization via CLI yet; set in GUI if needed.
set -euo pipefail

LMS="${LMS:-$HOME/.lmstudio/bin/lms}"
CTX="${CTX:-131072}"           # 128K default; set CTX=262144 for 256K but risk OOM on Gemma
GPU="${GPU:-max}"
MANIFEST="${MANIFEST:-security/model-checksums.yml}"

[[ -x "$LMS" ]] || { echo "LM Studio CLI missing: $LMS" >&2; exit 1; }
[[ -f "$MANIFEST" ]] || { echo "Manifest missing: $MANIFEST" >&2; exit 1; }

# Emit pipe-separated "name|lms-path|role" rows for main role models (skip drafts + embed).
UV="${UV:-$HOME/.local/bin/uv}"; command -v "$UV" >/dev/null || UV=uv
"$UV" run --quiet --with pyyaml python - "$MANIFEST" <<'PY' > /tmp/aiforge-roles.tsv
import re, sys, yaml
from pathlib import Path
doc = yaml.safe_load(Path(sys.argv[1]).read_text()) or {}
ROLES = {"sr-developer", "tester", "sr-architect"}
for m in doc.get("models") or []:
    if "Speculative" in (m.get("role_rationale") or ""):
        continue
    assigned = set(m.get("assigned_to") or []) & ROLES
    if not assigned:
        continue
    role = next(iter(assigned))
    path = m["path"].rstrip("/")
    path = re.sub(r".*/\.lmstudio/models/", "", path)
    print(f"{m['name']}|{path}|{role}")
PY

while IFS='|' read -r name path role; do
  echo ">>> load $name (role=$role) ctx=$CTX gpu=$GPU"
  "$LMS" load "$path" \
    --gpu "$GPU" \
    --context-length "$CTX" \
    --identifier "$role" \
    || echo "WARN: load failed for $name; continuing" >&2
done < /tmp/aiforge-roles.tsv

rm -f /tmp/aiforge-roles.tsv

echo
echo "Loaded role models. Current:"
"$LMS" ps
