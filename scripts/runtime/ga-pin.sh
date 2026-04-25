#!/usr/bin/env bash
# Pin or check the GenericAgent SHA in .aiforge/ga-version.lock.
#
# Usage:
#   ./scripts/runtime/ga-pin.sh           # pin current GA HEAD into lock
#   ./scripts/runtime/ga-pin.sh --check   # exit 1 if live GA != lock
#   ./scripts/runtime/ga-pin.sh --show    # print live + lock + diff
#
# Run on the host where GA is deployed (NUC for prod, MS for legacy).
# Lock format: "<sha>  # <YYYY-MM-DD>  pinned by <user>" (single line).
set -euo pipefail

GA_DIR="${AIFORGE_GA_DIR:-/home/mani/genericagent}"
[ -d "$GA_DIR" ] || GA_DIR="/Users/manikanta/genericagent"
[ -d "$GA_DIR" ] || GA_DIR="$HOME/genericagent"
[ -d "$GA_DIR" ] || { echo "ERROR: no GA dir found"; exit 1; }

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
LOCK_DIR="$REPO_ROOT/.aiforge"
LOCK_FILE="$LOCK_DIR/ga-version.lock"

LIVE_SHA="$(git -C "$GA_DIR" rev-parse --short HEAD)"
LIVE_FULL="$(git -C "$GA_DIR" rev-parse HEAD)"

case "${1:-pin}" in
  --show|show)
    echo "GA dir:    $GA_DIR"
    echo "Live SHA:  $LIVE_SHA  ($LIVE_FULL)"
    if [ -f "$LOCK_FILE" ]; then
      echo "Lock:      $(head -1 "$LOCK_FILE")"
      LOCK_SHA="$(head -1 "$LOCK_FILE" | awk '{print $1}')"
      if [ "$LOCK_SHA" = "$LIVE_SHA" ]; then
        echo "Status:    PINNED, in sync"
      else
        echo "Status:    DRIFT — live $LIVE_SHA != lock $LOCK_SHA"
      fi
    else
      echo "Lock:      (none)"
    fi
    ;;
  --check|check)
    if [ ! -f "$LOCK_FILE" ]; then
      echo "ERROR: no lock at $LOCK_FILE — run \`./scripts/runtime/ga-pin.sh\` first"
      exit 1
    fi
    LOCK_SHA="$(head -1 "$LOCK_FILE" | awk '{print $1}')"
    if [ "$LOCK_SHA" != "$LIVE_SHA" ]; then
      echo "DRIFT: live $LIVE_SHA != lock $LOCK_SHA"
      echo "  fix: re-run smoke gate (F-suite), then \`./scripts/runtime/ga-pin.sh\` to update"
      exit 1
    fi
    echo "OK: GA pinned at $LOCK_SHA"
    ;;
  --pin|pin|"")
    mkdir -p "$LOCK_DIR"
    echo "$LIVE_SHA  # $(date +%Y-%m-%d)  pinned by ${USER:-unknown}  full=$LIVE_FULL" > "$LOCK_FILE"
    echo "Pinned GA at $LIVE_SHA → $LOCK_FILE"
    ;;
  *)
    echo "usage: $0 [pin|--show|--check]"
    exit 2
    ;;
esac
