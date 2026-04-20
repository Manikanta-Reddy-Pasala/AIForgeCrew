#!/usr/bin/env bash
# scripts/install-sync-memory-agent.sh — install/uninstall the daily
# launchd agent that rsyncs ~/.claude/memory to Mac Studio.
#
# Usage:
#   bash scripts/install-sync-memory-agent.sh install   # default
#   bash scripts/install-sync-memory-agent.sh uninstall
#   bash scripts/install-sync-memory-agent.sh status
#   bash scripts/install-sync-memory-agent.sh run       # trigger a one-shot run now
set -euo pipefail

ACTION="${1:-install}"
REPO_DIR="${REPO_DIR:-$HOME/Documents/codeRepo/AIForgeCrew}"
LABEL="com.aiforge.sync-memory"
PLIST_SRC="$REPO_DIR/scripts/launchd/$LABEL.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs"
LOG_FILE="$LOG_DIR/aiforge-sync-memory.log"
# launchd can't read ~/Documents (TCC). Stage script in a TCC-unprotected path.
STAGE_DIR="$HOME/.local/libexec/aiforge"
STAGE_SCRIPT="$STAGE_DIR/sync-memory.sh"

[[ "$(uname -s)" == "Darwin" ]] || { echo "launchd is macOS only" >&2; exit 1; }
[[ -f "$PLIST_SRC" ]] || { echo "plist template missing at $PLIST_SRC" >&2; exit 1; }

case "$ACTION" in
  install)
    mkdir -p "$(dirname "$PLIST_DST")" "$LOG_DIR" "$STAGE_DIR"
    # Copy script out of ~/Documents (TCC-protected) to ~/.local (readable by launchd)
    cp "$REPO_DIR/scripts/sync-memory.sh" "$STAGE_SCRIPT"
    chmod +x "$STAGE_SCRIPT"
    # Expand placeholders — stage path, not repo path
    sed \
      -e "s|{{REPO_DIR}}|$STAGE_DIR|g" \
      -e "s|{{SYNC_SCRIPT}}|$STAGE_SCRIPT|g" \
      -e "s|{{LOG_FILE}}|$LOG_FILE|g" \
      "$PLIST_SRC" > "$PLIST_DST"
    chmod 644 "$PLIST_DST"
    launchctl unload "$PLIST_DST" 2>/dev/null || true
    launchctl load "$PLIST_DST"
    echo "installed → $PLIST_DST"
    echo "staged    → $STAGE_SCRIPT"
    echo "logs      → $LOG_FILE"
    echo "schedule  → daily at 02:00 local time"
    echo
    echo "Note: if sync fails with 'Operation not permitted' on ~/.claude access,"
    echo "grant Full Disk Access to /bin/bash in System Settings → Privacy & Security."
    ;;
  uninstall)
    if [[ -f "$PLIST_DST" ]]; then
      launchctl unload "$PLIST_DST" 2>/dev/null || true
      rm -f "$PLIST_DST"
      echo "removed → $PLIST_DST"
    else
      echo "not installed"
    fi
    ;;
  status)
    if launchctl list | awk '{print $3}' | grep -qx "$LABEL"; then
      echo "loaded:"
      launchctl list | awk -v l="$LABEL" '$3==l {print "  PID=" $1 "  exit=" $2 "  label=" $3}'
      echo
      echo "last 20 log lines:"
      tail -20 "$LOG_FILE" 2>/dev/null || echo "  (no log yet)"
    else
      echo "not loaded"
    fi
    ;;
  run)
    # Trigger immediate run (bypasses schedule)
    launchctl start "$LABEL"
    echo "triggered — tail log: tail -f $LOG_FILE"
    ;;
  *) echo "usage: $0 {install|uninstall|status|run}" >&2; exit 2 ;;
esac
