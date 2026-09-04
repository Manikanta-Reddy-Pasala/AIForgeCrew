#!/usr/bin/env bash
# Write LM Studio per-model default configs that enable 8-bit MLX KV
# cache quantization for the large-context Doer model. This HALVES the
# KV-cache RAM footprint (an 85K-token coder prompt = ~52G KV at 8-bit;
# ~104G unquantized would OOM the 96G Mac Studio with the judge model
# co-resident — empirically the only thing keeping the box alive, see
# llm-bench REPORT 2026-06-13).
#
# LM Studio applies "user-concrete-model-default-config" on EVERY load
# (CLI `lms load` included), so this survives idle-unload + lms-ensure
# reloads without a per-load flag (the CLI has none).
#
# 8-bit KV quant is near-lossless; do NOT drop to 4-bit for the Doer
# (code generation degrades). Vision-language models (e.g. nex-n2-mini,
# an mlx-vlm) do NOT support KV quant — the batched vision path raises
# "does not support KV cache quantization yet"; skip them (their small
# 32K KV needs no help anyway).
#
#   AIFORGE_LMS_HOST   ssh target (default manikanta@192.168.70.185)
#   KV_MODELS          "publisher/ModelDir:ctx:bits" list, comma-sep
#                      default: lmstudio-community/Qwen3-Coder-Next-MLX-4bit:262144:8
set -euo pipefail

HOST="${AIFORGE_LMS_HOST:-manikanta@192.168.70.185}"
KV_MODELS="${KV_MODELS:-lmstudio-community/Qwen3-Coder-Next-MLX-4bit:262144:8}"

IFS=',' read -ra SPECS <<< "$KV_MODELS"
for spec in "${SPECS[@]}"; do
    spec="$(echo "$spec" | xargs)"
    [[ -n "$spec" ]] || continue
    rel="${spec%%:*}"; rest="${spec#*:}"
    ctx="${rest%%:*}"; bits="${rest#*:}"
    pub="${rel%%/*}"; dir="${rel##*/}"
    echo "configuring KV quant ${bits}-bit ctx=${ctx} for ${rel}"
    ssh -o BatchMode=yes -o ConnectTimeout=10 "$HOST" \
        "CFGDIR=~/.lmstudio/.internal/user-concrete-model-default-config/${pub}; \
         mkdir -p \"\$CFGDIR\"; \
         cat > \"\$CFGDIR/${dir}.json\" << JSON
{
    \"preset\": \"\",
    \"operation\": { \"fields\": [] },
    \"load\": {
        \"fields\": [
            { \"key\": \"llm.load.contextLength\", \"value\": ${ctx} },
            { \"key\": \"llm.load.mlx.kvCacheQuantization\", \"value\": { \"enabled\": true, \"bits\": ${bits}, \"groupSize\": 64, \"quantizedStart\": 0 } }
        ]
    }
}
JSON
         echo \"  wrote \$CFGDIR/${dir}.json\""
done
echo "done — reload affected models (lms-ensure or lms load) to apply"
