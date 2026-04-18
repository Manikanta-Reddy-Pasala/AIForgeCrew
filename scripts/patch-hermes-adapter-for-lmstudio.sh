#!/usr/bin/env bash
# scripts/patch-hermes-adapter-for-lmstudio.sh — patch hermes-paperclip-adapter
# constants to accept `lmstudio` as a valid provider + map our local model
# prefixes to lmstudio.
#
# Why: adapter's VALID_PROVIDERS omits lmstudio; MODEL_PREFIX_PROVIDER_HINTS
# routes "zai-org/..." and similar to cloud providers → auth fails.
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "patch: macOS only" >&2; exit 1
fi

ADAPTER_JS="$HOME/.hermes/node/lib/node_modules/hermes-paperclip-adapter/dist/shared/constants.js"
[[ -f "$ADAPTER_JS" ]] || { echo "adapter not found at $ADAPTER_JS" >&2; exit 1; }

# Back up once.
[[ -f "$ADAPTER_JS.orig" ]] || cp "$ADAPTER_JS" "$ADAPTER_JS.orig"

if grep -q '"lmstudio"' "$ADAPTER_JS"; then
  echo "[skip] adapter already patched for lmstudio"
  exit 0
fi

python3 - "$ADAPTER_JS" <<'PY'
import re, sys
p = sys.argv[1]
src = open(p).read()

# 1. Add "lmstudio" to VALID_PROVIDERS array (insert before closing ]).
src = re.sub(
    r'(export const VALID_PROVIDERS = \[[^\]]*?"kilocode",)(\s*\])',
    r'\1\n    "lmstudio",\2',
    src, count=1, flags=re.S,
)

# 2. Insert local-model prefix hints at top of MODEL_PREFIX_PROVIDER_HINTS.
hints_insert = (
    "    // AIForgeCrew override: local LM Studio ids → lmstudio\n"
    '    ["zai-org/", "lmstudio"],\n'
    '    ["qwen3.6-", "lmstudio"],\n'
    '    ["qwen3-0.6b", "lmstudio"],\n'
    '    ["gemma-4-31b-it", "lmstudio"],\n'
    '    ["gemma-4-e2b-it", "lmstudio"],\n'
)
src = re.sub(
    r'(export const MODEL_PREFIX_PROVIDER_HINTS = \[\n)',
    r'\1' + hints_insert,
    src, count=1,
)

open(p, "w").write(src)
print("patched:", p)
PY

echo
echo "Verification:"
grep -n '"lmstudio"' "$ADAPTER_JS" | head -10
echo
echo "Restart Paperclip on Mac Studio Terminal to pick up patched adapter:"
echo "  make paperclip-stop && bash scripts/paperclip-start.sh"
