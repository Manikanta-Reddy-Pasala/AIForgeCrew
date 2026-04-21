#!/usr/bin/env bash
# Install the v5 tick-<role> LaunchAgents and disable legacy ones.
# Runs ON Mac Studio. Idempotent.
set -euo pipefail

[[ "$(uname -s)" == "Darwin" ]] || { echo "macOS only" >&2; exit 1; }

SRC="${SRC:-$HOME/AIForgeCrew/scripts/runtime}"
DST="$HOME/Library/LaunchAgents"

mkdir -p "$HOME/.aiforge/logs"
mkdir -p "$DST"

echo ">>> stopping legacy LaunchAgents"
for legacy in \
  com.aiforge.paperclip com.aiforge.paperclip-tunnel com.aiforge.hermes-dashboard
do
  launchctl bootout "gui/$(id -u)/${legacy}" 2>/dev/null || true
done

echo ">>> installing v5 LaunchAgents"
for role in architect sr_developer developer fact_extract; do
  label="com.aiforge.tick-${role}"
  src="${SRC}/${label}.plist"
  dst="${DST}/${label}.plist"
  [[ -f "$src" ]] || { echo "missing $src"; continue; }
  cp "$src" "$dst"

  launchctl bootout "gui/$(id -u)/${label}" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$dst"
  echo "  loaded $label"
done

echo
echo "Verify:"
echo "  launchctl list | grep aiforge.tick"
echo "  tail -f ~/.aiforge/logs/orchestrator-*.ndjson | jq -c '{ts,role,ticket,event,tool,dur_ms}'"
