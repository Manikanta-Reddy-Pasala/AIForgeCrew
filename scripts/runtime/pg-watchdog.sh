#!/usr/bin/env bash
# Postgres watchdog — keeps Homebrew postgresql@16 alive at 127.0.0.1:5432.
# Clears stale postmaster.pid from unclean shutdowns + restarts via brew.
# Runs under launchd with KeepAlive=true; if this script exits, launchd
# respawns (belt + braces).
set -uo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
LOG="$HOME/.aiforge/logs/pg-watchdog.log"
PG_DATA="/opt/homebrew/var/postgresql@16"
BREW_SVC="postgresql@16"

mkdir -p "$(dirname "$LOG")"

log() { printf '[%s] %s\n' "$(date -u +%FT%TZ)" "$*" >> "$LOG"; }

clear_stale_pid() {
  local pidfile="$PG_DATA/postmaster.pid"
  [[ -f "$pidfile" ]] || return 0
  local pid
  pid=$(head -1 "$pidfile" 2>/dev/null || echo "")
  if [[ -z "$pid" ]] || ! kill -0 "$pid" 2>/dev/null; then
    log "removing stale postmaster.pid (pid=$pid not alive)"
    rm -f "$pidfile"
  fi
}

start_pg() {
  clear_stale_pid
  log "starting $BREW_SVC"
  brew services restart "$BREW_SVC" >> "$LOG" 2>&1 || true
  sleep 4
}

# Initial ensure.
start_pg

while true; do
  if ! pg_isready -h 127.0.0.1 -p 5432 -q -t 3 2>/dev/null; then
    log "pg_isready failed — attempting restart"
    start_pg
  fi
  sleep 30
done
