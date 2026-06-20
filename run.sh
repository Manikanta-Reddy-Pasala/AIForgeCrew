#!/usr/bin/env bash
# run.sh — one-command boot for AIForge (deploy-anywhere).
#
#   git clone … && cd AIForgeCrew && ./run.sh
#
# Brings up a single process that serves the web UI + REST/SSE API on
# http://127.0.0.1:8799. Defaults to ZERO external infra: embedded
# SQLite tickets + SQLite memory, no Postgres/Neo4j/sidecars required.
# Point it at a model on the home page (http://127.0.0.1:8799/ui/).
#
# Flags:
#   --dev        uvicorn --reload (hot reload for development)
#   --port N     listen port (default 8799)
#   --host H     bind host (default 127.0.0.1)
#   --skip-web   don't (re)build the web UI
#
# ⚠️  By default the Chat agent has FULL filesystem + shell access on
#     this machine (no sandbox). Set AIFORGE_WORKSPACE_DIR=/path to clamp
#     it, or run inside a container for shared/untrusted deploys.
set -euo pipefail

cd "$(dirname "$0")"

PORT=8799
HOST=127.0.0.1
DEV=0
SKIP_WEB=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dev) DEV=1 ;;
    --skip-web) SKIP_WEB=1 ;;
    --port) PORT="$2"; shift ;;
    --host) HOST="$2"; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
  shift
done

# ── Python env ────────────────────────────────────────────────────────
if ! command -v uv >/dev/null 2>&1; then
  echo "==> 'uv' not found. Install it: https://docs.astral.sh/uv/  (curl -LsSf https://astral.sh/uv/install.sh | sh)" >&2
  exit 1
fi

if [[ ! -d .venv ]]; then
  echo "==> creating .venv"
  uv venv .venv
fi
echo "==> installing python deps (editable)"
.venv/bin/uv pip install -e . >/dev/null

# ── Web UI build (optional) ───────────────────────────────────────────
if [[ $SKIP_WEB -eq 0 ]]; then
  if command -v npm >/dev/null 2>&1; then
    # Rebuild only when dist is missing or any source is newer than it.
    if [[ ! -d web/dist ]] || [[ -n "$(find web/src web/index.html web/package.json -newer web/dist/index.html 2>/dev/null | head -1)" ]]; then
      echo "==> building web UI"
      ( cd web && { [[ -d node_modules ]] || npm ci; } && npm run build )
    else
      echo "==> web UI up to date (use --skip-web to skip this check)"
    fi
  else
    echo "==> npm not found — skipping UI build (API will still serve; install Node to get the UI)" >&2
  fi
fi

# ── Launch ────────────────────────────────────────────────────────────
echo ""
echo "  AIForge → http://${HOST}:${PORT}/ui/"
echo "  storage: SQLite (set AIFORGE_PG_URL / NEO4J_URI for the pro backends)"
[[ -n "${AIFORGE_WORKSPACE_DIR:-}" ]] \
  && echo "  chat fs scope: ${AIFORGE_WORKSPACE_DIR}" \
  || echo "  chat fs scope: UNRESTRICTED (set AIFORGE_WORKSPACE_DIR to clamp)"
echo ""

RELOAD=()
[[ $DEV -eq 1 ]] && RELOAD=(--reload)
exec .venv/bin/uvicorn aiforge_core.api.api:app --host "$HOST" --port "$PORT" "${RELOAD[@]}"
