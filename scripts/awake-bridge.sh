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
# While it holds, it touches "<heartbeat file>.held": the box reads that to
# tell the user when NOTHING holds this machine awake under a run.
#
# It keeps watching while the container is down (a Docker Desktop restart, a
# restart policy or a reboot of the VM brings the box back without ./run.sh).
# AIFORGE_AWAKE_GIVE_UP_CHECKS=N (15 s each) makes it exit after N checks
# with the box down; 0 (the default) never.
set -uo pipefail

FILE="${1:?usage: awake-bridge.sh <heartbeat file> [container]}"
BOX="${2:-aiforge}"
STALE_S="${AIFORGE_AWAKE_STALE_S:-60}"
GIVE_UP="${AIFORGE_AWAKE_GIVE_UP_CHECKS:-0}"
[[ "$STALE_S" =~ ^[0-9]+$ ]] || STALE_S=60
[[ "$GIVE_UP" =~ ^[0-9]+$ ]] || GIVE_UP=0
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOLDER=""

_mtime() {                                   # epoch seconds, GNU or BSD stat
  stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null || echo 0
}

_release() {
  rm -f "$FILE.held" 2>/dev/null || true
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
    (( GIVE_UP > 0 && misses >= GIVE_UP )) && exit 0
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
    sleep 1
    if kill -0 "$HOLDER" 2>/dev/null; then
      touch "$FILE.held" 2>/dev/null || true
    else
      rm -f "$FILE.held" 2>/dev/null || true   # no power API here
    fi
  else
    _release
  fi
  sleep 15
done
