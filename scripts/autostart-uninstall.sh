#!/usr/bin/env bash
# scripts/autostart-uninstall.sh — remove AIForgeCrew LaunchAgents.
set -euo pipefail

UID_=$(id -u)
LA="$HOME/Library/LaunchAgents"
for svc in caffeinate lmstudio paperclip hermes-dashboard; do
  label="com.aiforge.${svc}"
  launchctl bootout "gui/$UID_/$label" 2>/dev/null && echo "booted-out $label" || echo "$label not loaded"
  rm -f "$LA/com.aiforge.${svc}.plist" "$LA/com.aiforge.${svc}.sh"
done
echo "AIForgeCrew LaunchAgents removed."
