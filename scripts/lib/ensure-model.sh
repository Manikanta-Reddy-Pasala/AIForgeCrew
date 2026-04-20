#!/usr/bin/env bash
# Ensure exactly one LM Studio model loaded w/ correct ctx + Hermes cache synced.
# Usage: ensure-model.sh <MODEL_ID> <CTX_TOKENS>
# Handles: LM Studio silent ctx reduction, JIT-loaded clones (:2, :3 suffix), stale Hermes cache.
set -uo pipefail

MODEL="${1:?usage: ensure-model.sh <MODEL> <CTX>}"
CTX="${2:?usage: ensure-model.sh <MODEL> <CTX>}"

SSH_HOST="${SSH_HOST:-manikanta@192.168.70.185}"
remote() { ssh -o IdentitiesOnly=yes -o ServerAliveInterval=60 -i ~/.ssh/id_ed25519 "$SSH_HOST" "$@"; }

# `lms ps` columns: IDENTIFIER MODEL STATUS SIZE_NUM SIZE_UNIT CONTEXT PARALLEL DEVICE TTL ...
# CONTEXT is column 6 (awk counts from 1).
read_state() {
  remote "~/.lmstudio/bin/lms ps 2>/dev/null | awk 'NR>1 && \$1 != \"\" && \$1 != \"IDENTIFIER\" {print \$1 \"|\" \$6}'" || true
}

STATE=$(read_state)
LOADED_CTX=$(echo "$STATE" | awk -F'|' -v m="$MODEL" '$1==m {print $2}' | head -1)
CLONES=$(echo "$STATE" | awk -F'|' -v m="$MODEL" '$1 ~ "^"m":" {print $1}')

if [[ "$LOADED_CTX" == "$CTX" && -z "$CLONES" ]]; then
  echo "  ensure-model: $MODEL already loaded @ $CTX (no clones) ✓"
else
  echo "  ensure-model: $MODEL loaded_ctx='$LOADED_CTX' target='$CTX' clones='$CLONES' — reload"
  # Unload EVERYTHING to free RAM (guardrail is conservative on 96GB system)
  remote "~/.lmstudio/bin/lms unload --all 2>&1 | tail -1" || true
  sleep 3
  remote "~/.lmstudio/bin/lms load -y '$MODEL' -c $CTX --gpu max --ttl 86400 2>&1 | tail -2" || true
  sleep 2

  # Verify post-load — re-read state
  STATE2=$(read_state)
  ACTUAL=$(echo "$STATE2" | awk -F'|' -v m="$MODEL" '$1==m {print $2}' | head -1)
  if [[ "$ACTUAL" != "$CTX" ]]; then
    echo "  ensure-model: WARNING — requested $CTX, got '$ACTUAL'. LM Studio guardrail reduced."
    CTX="$ACTUAL"
  else
    echo "  ensure-model: $MODEL loaded @ $CTX ✓"
  fi
fi

# Sync Hermes ctx cache to actual loaded ctx
remote "python3 - <<PYEOF
from pathlib import Path
import re
p = Path.home()/'.hermes/context_length_cache.yaml'
if not p.exists():
    p.write_text('context_lengths:\n')
t = p.read_text()
for suffix in ('', '/'):
    key = '$MODEL@http://localhost:1234/v1' + suffix
    pat = re.compile(r'^(\s+)'+re.escape(key)+r':\s+\d+', re.MULTILINE)
    if pat.search(t):
        t = pat.sub(lambda m: f'{m.group(1)}{key}: $CTX', t)
    else:
        t = t.rstrip()+f'\n  {key}: $CTX\n'
p.write_text(t)
PYEOF
"
echo "  ensure-model: hermes ctx cache synced to $CTX"

# REST smoke test
RESP=$(remote "curl -s -w '%{http_code}' http://localhost:1234/v1/chat/completions -H 'Content-Type: application/json' -d '{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"ok\"}],\"max_tokens\":2}' -o /dev/null" || echo "000")
if [[ "$RESP" == "200" ]]; then
  echo "  ensure-model: REST smoke OK"
else
  echo "  ensure-model: REST smoke HTTP $RESP — model unhealthy"
  exit 1
fi
