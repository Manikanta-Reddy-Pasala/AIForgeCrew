#!/usr/bin/env bash
# Container entrypoint for AIForge "docker mode" — single-mode (embedded SQLite +
# scoped-OKR memory), all deps baked into the image. Mirrors the boot half of
# run.sh (converge → api); it does NOT install a toolchain (the image already
# has python deps, aider, semantic, node-built UI). All state lives under
# AIFORGE_CONFIG_DIR, a host-mounted volume, so it persists across restarts.
set -euo pipefail

: "${AIFORGE_CONFIG_DIR:=/data/aiforge}"
: "${AIFORGE_PORT:=8799}"
: "${AIFORGE_BIND_HOST:=127.0.0.1}"
export AIFORGE_CONFIG_DIR AIFORGE_PORT AIFORGE_BIND_HOST
mkdir -p "$AIFORGE_CONFIG_DIR"

# The whole host filesystem is mounted at /host (see docker-compose.yml) so the
# agent operates on real repos; trust every git repo under it (uid mismatch on a
# bind mount otherwise trips git's "dubious ownership" and fails edits silently).
git config --system --add safe.directory '*' 2>/dev/null || true

# If the embed model was baked into the image (HF_HOME cache non-empty), run HF
# fully OFFLINE so the first chat message never blocks on a network fetch. If it
# was NOT baked (PREFETCH_EMBED_MODEL=0), stay online so it can download once.
if [[ -n "$(ls -A "${HF_HOME:-/opt/hf-cache}" 2>/dev/null)" ]]; then
  export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
fi

# Converge: run the same startup migrations the native path does (idempotent) —
# briefs/captures folders, contradiction/lint, any prior-install migration.
# Single-mode: no PG/Neo4j to stand up. Best-effort; never block the api.
echo "==> converge (startup migrations, single-mode)…"
python -m aiforge_core.deploy.converge ${AIFORGE_MIGRATE:+--force} || true

# Optional: a runner loop alongside the api so queued tickets get processed.
# AIFORGE_RUNNER_CONCURRENCY=0 (default) → api only. N>0 → N claim-run loops.
_N="${AIFORGE_RUNNER_CONCURRENCY:-0}"
if [[ "$_N" =~ ^[0-9]+$ ]] && (( _N > 0 )); then
  echo "==> starting $_N runner loop(s)…"
  for _i in $(seq 1 "$_N"); do
    ( while true; do
        python -m aiforge_core.runtime.adk_runner || true
        sleep "${AIFORGE_RUNNER_POLL_SEC:-10}"
      done ) &
  done
fi

echo "==> serving API on ${AIFORGE_BIND_HOST}:${AIFORGE_PORT}"
exec python -m uvicorn aiforge_core.api.api:app \
  --host "$AIFORGE_BIND_HOST" --port "$AIFORGE_PORT"
