#!/usr/bin/env bash
# scripts/paperclip-install-agent-instructions.sh — deploy AGENTS.md role
# instructions from this repo into Paperclip's per-agent instructions dir.
#
# Paperclip reads AGENTS.md from
#   ~/.paperclip/instances/default/companies/<cid>/agents/<aid>/instructions/AGENTS.md
# on each heartbeat run. This script syncs our agents/<role>/AGENTS.md into
# the right paths so agents obey the DESIGN §4 pipeline (Tester → Sr Dev
# → Sr Arch with GitHub PR creation).
#
# Run on Mac Studio. Idempotent.
set -euo pipefail

[[ "$(uname -s)" == "Darwin" ]] || { echo "macOS only" >&2; exit 1; }

REPO="${REPO:-$HOME/AIForgeCrew}"
CID="${CID:-fd294bd0-2f65-405f-b443-fb41d66226fb}"
INST_ROOT="$HOME/.paperclip/instances/default/companies/$CID/agents"

command -v jq >/dev/null || { echo "jq required" >&2; exit 1; }

# Map role → Paperclip agent UUID. Read from Paperclip API.
echo ">>> mapping roles → agent IDs"
AGENTS=$(curl -sS "http://localhost:3100/api/companies/$CID/agents")
em_id=$(echo "$AGENTS"       | jq -r '[.[] | select(.role=="pm")][0].id')
tester_id=$(echo "$AGENTS"   | jq -r '[.[] | select(.role=="qa")][0].id')
dev_id=$(echo "$AGENTS"      | jq -r '[.[] | select(.role=="engineer")][0].id')
arch_id=$(echo "$AGENTS"     | jq -r '[.[] | select(.role=="cto")][0].id')

for var in em_id tester_id dev_id arch_id; do
  eval v=\$$var
  [[ -n "$v" && "$v" != "null" ]] || { echo "missing agent: $var" >&2; exit 1; }
done

# source role dir → destination agent uuid
declare -a PAIRS=(
  "em:$em_id"
  "tester:$tester_id"
  "sr-developer:$dev_id"
  "sr-architect:$arch_id"
)

for pair in "${PAIRS[@]}"; do
  IFS=':' read -r role aid <<< "$pair"
  src="$REPO/agents/$role/AGENTS.md"
  dst_dir="$INST_ROOT/$aid/instructions"
  dst="$dst_dir/AGENTS.md"

  [[ -f "$src" ]] || { echo "  [skip] $role — source missing: $src"; continue; }

  mkdir -p "$dst_dir"
  cp "$src" "$dst"
  echo "  [ok]   $role ($aid) ← $src"

  # Also copy any other role files (permissions.yml etc.) if present.
  for extra in "$REPO/agents/$role/"*.yml; do
    [[ -f "$extra" ]] || continue
    cp "$extra" "$dst_dir/"
    echo "         + $(basename "$extra")"
  done
done

echo
echo "Agent instructions installed. Paperclip picks these up on next heartbeat."
echo "Verify with: ls $INST_ROOT/*/instructions/"
