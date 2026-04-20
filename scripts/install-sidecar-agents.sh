#!/usr/bin/env bash
# Install/uninstall/status the embed + rerank sidecar LaunchAgents on Mac Studio.
# Run ON the Mac Studio (locally). Not over SSH.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LA_DIR="$HOME/Library/LaunchAgents"
PLIST_DIR="$REPO_ROOT/scripts/launchd"

EMBED_PLIST="com.aiforge.embed-sidecar.plist"
RERANK_PLIST="com.aiforge.rerank-sidecar.plist"

action="${1:-install}"

install_one() {
  local name="$1"
  cp "$PLIST_DIR/$name" "$LA_DIR/$name"
  launchctl unload "$LA_DIR/$name" 2>/dev/null || true
  launchctl load   "$LA_DIR/$name"
  echo "  loaded: $name"
}

uninstall_one() {
  local name="$1"
  launchctl unload "$LA_DIR/$name" 2>/dev/null || true
  rm -f "$LA_DIR/$name"
  echo "  removed: $name"
}

status_one() {
  local label="$1"
  launchctl list | awk -v l="$label" '$3 == l { print "  " l " pid=" $1 " exit=" $2 }'
}

case "$action" in
  install)
    mkdir -p "$LA_DIR" "$HOME/.aiforge/logs"
    install_one "$EMBED_PLIST"
    install_one "$RERANK_PLIST"
    echo
    echo "agents loaded. status:"
    status_one com.aiforge.embed-sidecar
    status_one com.aiforge.rerank-sidecar
    ;;
  uninstall)
    uninstall_one "$EMBED_PLIST"
    uninstall_one "$RERANK_PLIST"
    ;;
  status)
    status_one com.aiforge.embed-sidecar
    status_one com.aiforge.rerank-sidecar
    ;;
  restart)
    launchctl kickstart -k "gui/$(id -u)/com.aiforge.embed-sidecar"  || true
    launchctl kickstart -k "gui/$(id -u)/com.aiforge.rerank-sidecar" || true
    ;;
  *)
    echo "usage: $0 {install|uninstall|status|restart}" >&2
    exit 1
    ;;
esac
