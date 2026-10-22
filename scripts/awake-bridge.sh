#!/usr/bin/env bash
# Host side of keep-awake for the docker sandbox.
#
# The sandbox is a Linux box with no power API of the host's: a Mac that goes
# to sleep pauses the whole Docker VM under a running chat, and the turn comes
# back as a dead model socket. Inside the box, aiforge_core/runtime/keep_awake.py
# touches a heartbeat file in the mounted config dir while a run is in flight
# (every 20 s) and removes it after. This script, started on the HOST by
# ./run.sh, holds the host's own assertion (scripts/keep-awake.sh: caffeinate,
# systemd-inhibit or SetThreadExecutionState) while that file is fresh, and
# lets go when it is gone or stale — a box killed -9 stops touching it.
#
# The screen still locks normally. A laptop with its lid closed on battery may
# still sleep (caffeinate -s holds only on AC power).
#
#   scripts/awake-bridge.sh <heartbeat file> [container name]
#
# It exits by itself when the container is no longer running.
set -uo pipefail

FILE="${1:?usage: awake-bridge.sh <heartbeat file> [container]}"
BOX="${2:-aiforge}"
STALE_S="${AIFORGE_AWAKE_STALE_S:-60}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOLDER=""

_mtime() {                                   # epoch seconds, GNU or BSD stat
  stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null || echo 0
}

_release() {
  if [[ -n "$HOLDER" ]]; then
    kill "$HOLDER" 2>/dev/null || true
    wait "$HOLDER" 2>/dev/null || true
    HOLDER=""
    echo "$(date '+%F %T') awake-bridge: released"
  fi
}
trap '_release; exit 0' EXIT INT TERM

_box_up() {
  [[ "$(docker inspect -f '{{.State.Running}}' "$BOX" 2>/dev/null)" == "true" ]]
}

misses=0
while true; do
  if _box_up; then
    misses=0
  else
    misses=$((misses + 1))
    (( misses >= 4 )) && exit 0             # the box is gone: so is this
  fi
  fresh=0
  if [[ -f "$FILE" ]]; then
    age=$(( $(date +%s) - $(_mtime "$FILE") ))
    (( age <= STALE_S )) && fresh=1
  fi
  if (( fresh )); then
    if [[ -z "$HOLDER" ]] || ! kill -0 "$HOLDER" 2>/dev/null; then
      "$HERE/keep-awake.sh" >/dev/null 2>&1 &
      HOLDER=$!
      echo "$(date '+%F %T') awake-bridge: holding (a run is in flight)"
    fi
  else
    _release
  fi
  sleep 15
done
