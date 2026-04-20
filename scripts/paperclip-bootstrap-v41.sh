#!/usr/bin/env bash
# scripts/paperclip-bootstrap-v41.sh — register/update the 4 v4.1 agents in
# Paperclip, install their instruction files, and pause unused v3 agents.
#
# Mapping (Paperclip role | name | model | status):
#   architect     → "Architect"    claude-opus-4-7                (idle)
#   sr_developer  → "Sr Developer" gemma-4-31b-it                 (idle)
#   developer     → "Developer"    qwen3-coder-next               (idle)
#   fact_extract  → "Fact Extract" qwen3-4b-thinking-2507         (idle)
#
# Unused v3 agents (Tester, EM as QA-role) → paused.
# Run ON Mac Studio. Idempotent.
set -euo pipefail

[[ "$(uname -s)" == "Darwin" ]] || { echo "macOS only" >&2; exit 1; }

REPO="${REPO:-$HOME/AIForgeCrew}"
CID="${CID:-fd294bd0-2f65-405f-b443-fb41d66226fb}"
PG="PGPASSWORD=paperclip /Users/manikanta/.pg0/installation/18.1.0/bin/psql -h 127.0.0.1 -p 54329 -U paperclip -d paperclip"
INST_ROOT="$HOME/.paperclip/instances/default/companies/$CID/agents"

echo ">>> resolving existing agent UUIDs"
# Reuse existing UUIDs so prior references (branches, audit trails) stay valid.
ARCHITECT_ID="35760e2f-4cef-4013-9aff-d93592b5f71e"   # was EM (claude)
SRDEV_ID="28b8c064-bfcf-44e1-9e91-e37c39e0097c"       # was Sr Developer (gemma)
DEV_ID="e0502e94-0608-4fb9-9afa-b70d8dbf014a"         # was Developer (qwen-coder)
FACTX_ID="$($PG -Atc "SELECT id FROM agents WHERE company_id='$CID' AND role='fact_extract' LIMIT 1" 2>/dev/null || true)"

if [[ -z "$FACTX_ID" ]]; then
  echo ">>> creating Fact Extract agent"
  FACTX_ID=$(uuidgen | tr 'A-Z' 'a-z')
  $PG -c "
  INSERT INTO agents (id, company_id, name, role, title, status, adapter_type, adapter_config, budget_monthly_cents, permissions, runtime_config)
  VALUES (
    '$FACTX_ID', '$CID', 'Fact Extract', 'fact_extract', 'Reflection Agent',
    'idle', 'hermes_local',
    '{\"model\":\"qwen3-4b-thinking-2507\",\"provider\":\"auto\",\"timeoutSec\":900,\"hermesCommand\":\"/Users/manikanta/.local/bin/hermes-serial\",\"persistSession\":false,\"instructionsBundleMode\":\"managed\",\"instructionsEntryFile\":\"AGENTS.md\"}',
    500, '{}', '{}'
  );"
fi

echo ">>> updating agent metadata"
$PG -c "
UPDATE agents SET name='Architect', role='architect', title='System Architect', status='idle',
  adapter_type='claude_local',
  adapter_config = adapter_config || '{\"model\":\"claude-opus-4-7\",\"provider\":\"claude-cloud\"}'
WHERE id='$ARCHITECT_ID';

UPDATE agents SET name='Sr Developer', role='sr_developer', title='Decomposition', status='idle',
  adapter_config = adapter_config || '{\"model\":\"gemma-4-31b-it\",\"provider\":\"auto\"}'
WHERE id='$SRDEV_ID';

UPDATE agents SET name='Developer', role='developer', title='Implementation', status='idle',
  adapter_config = adapter_config || '{\"model\":\"qwen3-coder-next\",\"provider\":\"auto\"}'
WHERE id='$DEV_ID';

-- Pause v3-era agents we're not using in v4.1
UPDATE agents SET status='paused', pause_reason='superseded by v4.1', paused_at=now()
WHERE company_id='$CID'
  AND role IN ('qa','pm')
  AND status != 'paused';

-- Pause the gemma-26b Sr Architect (we're using claude for Architect now)
UPDATE agents SET status='paused', pause_reason='v4.1 uses claude for Architect', paused_at=now()
WHERE id='0e173374-287c-4595-bf46-6ba26c11035f' AND status != 'paused';
"

echo ">>> deploying instruction files"
deploy() {
  local role="$1" aid="$2"
  local src_dir="$REPO/agents/$role"
  local dst_dir="$INST_ROOT/$aid/instructions"
  [[ -d "$src_dir" ]] || { echo "  [skip] $role — source missing"; return; }
  mkdir -p "$dst_dir"
  cp "$src_dir/system-prompt.md" "$dst_dir/AGENTS.md"
  for extra in "$src_dir"/*.yml "$src_dir"/*.md; do
    [[ -f "$extra" ]] || continue
    [[ "$(basename "$extra")" == "system-prompt.md" ]] && continue
    cp "$extra" "$dst_dir/"
  done
  echo "  [ok] $role → $aid"
}

deploy architect    "$ARCHITECT_ID"
deploy sr-developer "$SRDEV_ID"
deploy developer    "$DEV_ID"
deploy fact-extract "$FACTX_ID"

echo
echo "v4.1 agent bootstrap complete."
echo "ARCHITECT_ID=$ARCHITECT_ID"
echo "SRDEV_ID=$SRDEV_ID"
echo "DEV_ID=$DEV_ID"
echo "FACTX_ID=$FACTX_ID"
