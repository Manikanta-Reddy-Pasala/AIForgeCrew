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
# KV-quant is now passed on the load line for text models (KV_BITS, default 4).
# If the installed lms rejects KV_FLAG, the load WARNs and continues; set in
# GUI as a fallback or update KV_FLAG to the version's actual token.
set -euo pipefail

LMS="${LMS:-$HOME/.lmstudio/bin/lms}"
# 2026-04-23: collapsed to 2 models (qwen3.6-35b-a3b + qwen3-coder-next) at 256K ctx.
# See docs/runtime/model-config.md / plist env AIFORGE_*_MODEL.
CTX="${CTX:-262144}"
GPU="${GPU:-max}"
# EVAL-3 finding (2026-04-23): default LM Studio TTL=1h w/ idle-unload
# kills mid-run multi-query agents. Pin for 12h by default; override w/ TTL=... .
TTL="${TTL:-43200}"
MANIFEST="${MANIFEST:-security/model-checksums.yml}"
# KV-cache quant. DEFAULT 0 (OFF): the installed LM Studio `lms load`
# CLI has NO KV-quant flag — it errors "unknown option
# '--kv-cache-quantization'" (verified live 2026-06-19), so enabling
# would break the load. Forward-compat hook only: set KV_BITS>0 ONLY
# against an lms build that accepts KV_FLAG. When >0, applied to TEXT
# models only (vision/embedding break under KV-quant, obs-28582).
KV_BITS="${KV_BITS:-0}"
KV_FLAG="--kv-cache-quantization"

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
    # Classify for KV-quant eligibility. Vision/embedding MLX models
    # break under KV-cache quantization (obs-28582) → kind!=text skips it.
    low = (m["name"] + " " + path).lower()
    if any(k in low for k in ("vision", "-vl", "vl-", "llava", "nex-n2-mini")):
        kind = "vision"
    elif "embed" in low:
        kind = "embedding"
    else:
        kind = "text"
    print(f"{m['name']}|{path}|{role}|{kind}")
PY

while IFS='|' read -r name path role kind; do
  kv=""
  if [[ "$KV_BITS" -gt 0 && "$kind" == "text" ]]; then
    kv="$KV_FLAG $KV_BITS"
  fi
  echo ">>> load $name (role=$role kind=$kind) ctx=$CTX gpu=$GPU ttl=${TTL}s kv=${kv:-none}"
  "$LMS" load "$path" \
    --gpu "$GPU" \
    --context-length "$CTX" \
    --ttl "$TTL" \
    --identifier "$role" \
    $kv \
    || echo "WARN: load failed for $name; continuing" >&2
done < /tmp/aiforge-roles.tsv

rm -f /tmp/aiforge-roles.tsv

echo
echo "Loaded role models. Current:"
"$LMS" ps
