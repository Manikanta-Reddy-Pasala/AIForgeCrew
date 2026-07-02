#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Run AIForge on the NUC in HYBRID mode: Postgres + Neo4j + embed + rerank stay
# in Docker, but the AGENT (api + ui + runner) runs on the HOST. This is the
# intended topology — the host api gets the host filesystem, host shell, and
# host toolchain (incl. the `graphify` CLI, which is not in the container), and
# it eliminates the Docker image-rebuild staleness (the host runs the git code
# directly, no COPY-layer cache to bust).
#
# Idempotent: safe to re-run. Leaves the dockerized infra untouched, stops only
# the api + runner containers (frees :8799 for the host process), then launches
# run.sh in hybrid mode under setsid so it survives an SSH disconnect.
#
# Usage (on the NUC, from the repo root):
#   ./scripts/deploy-nuc-hybrid.sh
# Env overrides:
#   AIFORGE_PORT (default 8799)   AIFORGE_BIND (default 0.0.0.0 — the tunnel)
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"

PORT="${AIFORGE_PORT:-8799}"
BIND="${AIFORGE_BIND:-0.0.0.0}"
LOG="/tmp/aiforge-hybrid.log"

echo "==> AIForge NUC hybrid launcher (repo: $ROOT)"

# 1) uv (run.sh needs it to build the host venv) --------------------------------
if ! command -v uv >/dev/null 2>&1; then
  echo "==> installing uv (missing)"
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
command -v uv >/dev/null 2>&1 || { echo "!! uv still not on PATH" >&2; exit 1; }

# 2) graphify — the host CLI the container never had (fixes Graphify=0) ---------
if [[ -z "${AIFORGE_GRAPHIFY_BIN:-}" ]]; then
  GB="$(command -v graphify || echo "$HOME/.local/bin/graphify")"
  [[ -x "$GB" ]] && export AIFORGE_GRAPHIFY_BIN="$GB"
fi
echo "==> graphify: ${AIFORGE_GRAPHIFY_BIN:-<not found — Graphify layer will skip>}"

# 3) Neo4j password: run.sh hybrid reads NEO4J_PASSWORD; derive it from a
#    compose-style NEO4J_AUTH=neo4j/<pw> if only that is set in .env. ----------
if [[ -z "${NEO4J_PASSWORD:-}" && -f .env ]]; then
  _auth="$(grep -E '^NEO4J_AUTH=' .env | head -1 | cut -d= -f2-)"
  [[ "$_auth" == */* ]] && export NEO4J_PASSWORD="${_auth#*/}"
fi

# 4) Host workspace = where repos to index live (container used /workspace) -----
export AIFORGE_HOST_WORKSPACE="${AIFORGE_HOST_WORKSPACE:-$ROOT/data/workspace}"
mkdir -p "$AIFORGE_HOST_WORKSPACE"

# 5) The tunnel (Cloudflare → nginx → WireGuard) hits a non-loopback bind; the
#    operator fronts it, so allow the unauth non-loopback bind. -----------------
export AIFORGE_ALLOW_UNAUTH_NONLOOPBACK=1
export AIFORGE_DOCKER_SUDO="${AIFORGE_DOCKER_SUDO:-auto}"

# 6) Free :8799 — stop the api + runner CONTAINERS (keep infra running). --------
echo "==> stopping docker api + runner containers (infra stays up)"
sudo docker compose stop api runner >/dev/null 2>&1 || true

# 7) Launch run.sh hybrid, detached, logging to $LOG. ---------------------------
echo "==> launching host api+ui+runner (hybrid) on ${BIND}:${PORT} → $LOG"
setsid env \
  AIFORGE_GRAPHIFY_BIN="${AIFORGE_GRAPHIFY_BIN:-}" \
  AIFORGE_HOST_WORKSPACE="$AIFORGE_HOST_WORKSPACE" \
  AIFORGE_ALLOW_UNAUTH_NONLOOPBACK=1 \
  NEO4J_PASSWORD="${NEO4J_PASSWORD:-}" \
  PATH="$PATH" \
  ./run.sh --host "$BIND" --port "$PORT" >"$LOG" 2>&1 < /dev/null &

echo "==> started (pid $!). Tail with:  tail -f $LOG"
echo "==> health (after ~build):        curl -s http://127.0.0.1:${PORT}/api/health"
