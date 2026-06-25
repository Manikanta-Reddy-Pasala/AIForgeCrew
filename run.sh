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
#   --docker     run the full Postgres+Neo4j stack via docker compose
#                instead of the local uvicorn. Stops any already-running
#                AIForge containers first, then `up -d --build` so a code
#                change rebuilds the image before (re)starting. Exits after.
#   --no-build   with --docker: skip the image rebuild (just (re)start)
#   --reset-config  wipe ~/.aiforge/agent_config.json (backs it up) so stale
#                per-role rows can't shadow the model you set next. Run once,
#                then reconfigure the model on the home page.
#   --test       probe the configured model endpoint with the current SSL
#                settings (OK/FAIL + error), then exit. Use to verify a
#                self-hosted HTTPS endpoint reaches AND its TLS is accepted.
#
# Self-hosted model over HTTPS with an internal/self-signed cert?
# Drop an `.env` (or `aiforge.env`) next to this script — it is sourced
# automatically. See `.env.example`. Relevant keys:
#   AIFORGE_LM_BASE_URL       https://your-box:1234/v1   (the model endpoint)
#   AIFORGE_LLM_SSL_VERIFY    false   (relax TLS for INTERNAL hosts only)
#   AIFORGE_LLM_CA_BUNDLE     /path/to/ca.pem  (preferred: keep verify ON)
#
# ⚠️  By default the Chat agent has FULL filesystem + shell access on
#     this machine (no sandbox). Set AIFORGE_WORKSPACE_DIR=/path to clamp
#     it, or run inside a container for shared/untrusted deploys.
set -euo pipefail

cd "$(dirname "$0")"

# ── Local env file (self-hosted endpoint + TLS toggle) ────────────────
# Source a committed-out `.env` / `aiforge.env` if present so `./run.sh`
# applies the operator's model base_url + SSL settings with NO manual
# `export`. `set -a` auto-exports every assignment in the file.
for _envf in .env aiforge.env; do
  if [[ -f "$_envf" ]]; then
    echo "==> loading env from $_envf"
    set -a; . "./$_envf"; set +a
    break
  fi
done

# Also source the UI-persisted runtime overrides (~/.aiforge/runtime.env),
# written by in-app toggles (LLM backend, Force full pipeline, …) so they
# survive a restart. Project .env above wins for keys it sets (sourced first).
_runtime_env="${AIFORGE_RUNTIME_ENV:-$HOME/.aiforge/runtime.env}"
if [[ -f "$_runtime_env" ]]; then
  echo "==> loading UI-persisted overrides from $_runtime_env"
  set -a; . "$_runtime_env"; set +a
fi

# Secure-by-default: verification stays ON unless the env file flips it.
# Exporting (without override) makes the value visible to the app + probe.
export AIFORGE_LLM_SSL_VERIFY="${AIFORGE_LLM_SSL_VERIFY:-true}"
[[ -n "${AIFORGE_LLM_CA_BUNDLE:-}" ]] && export AIFORGE_LLM_CA_BUNDLE

PORT=8799
HOST=127.0.0.1
DEV=0
SKIP_WEB=0
TEST=0
DOCKER=0
NO_BUILD=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dev) DEV=1 ;;
    --skip-web) SKIP_WEB=1 ;;
    --test) TEST=1 ;;
    --docker) DOCKER=1 ;;
    --no-build) NO_BUILD=1 ;;
    --reset-config) RESET_CONFIG=1 ;;
    --port) PORT="$2"; shift ;;
    --host) HOST="$2"; shift ;;
    -h|--help) sed -n '2,34p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
  shift
done

# ── Reset saved agent config (--reset-config) ─────────────────────────
# Wipe ~/.aiforge/agent_config.json so stale per-role rows can't shadow the
# endpoint you set next. Run ONCE, then reconfigure the model on the home
# page (or via env). Honours AIFORGE_CONFIG_DIR.
if [[ "${RESET_CONFIG:-0}" == "1" ]]; then
  _cfg_dir="${AIFORGE_CONFIG_DIR:-$HOME/.aiforge}"
  _cfg_file="$_cfg_dir/agent_config.json"
  if [[ -f "$_cfg_file" ]]; then
    mv -f "$_cfg_file" "$_cfg_file.bak.$(date +%s)" 2>/dev/null \
      && echo "==> agent config reset (backed up): $_cfg_file" \
      || { rm -f "$_cfg_file"; echo "==> agent config reset: $_cfg_file"; }
  else
    echo "==> no saved agent config to reset ($_cfg_file)"
  fi
fi

# ── Docker stack (--docker) ───────────────────────────────────────────
# Full Postgres + Neo4j + sidecars + api + runner via docker compose.
# Stop anything already running so a stale container can't shadow the new
# build, then `up -d --build`: Docker's layer cache means the image is
# rebuilt only when a source layer actually changed (the `COPY . .` layer
# invalidates on any code change). The SSL/model env vars sourced above
# flow into compose via its ${AIFORGE_LLM_SSL_VERIFY:-true} interpolation.
if [[ $DOCKER -eq 1 ]]; then
  if ! command -v docker >/dev/null 2>&1; then
    echo "==> 'docker' not found. Install Docker to use --docker." >&2; exit 1
  fi
  if docker compose version >/dev/null 2>&1; then
    DC=(docker compose)
  elif command -v docker-compose >/dev/null 2>&1; then
    DC=(docker-compose)
  else
    echo "==> docker compose plugin not found." >&2; exit 1
  fi
  echo "==> stopping any running AIForge containers"
  "${DC[@]}" down --remove-orphans || true
  if [[ $NO_BUILD -eq 1 ]]; then
    echo "==> starting (no rebuild)"
    "${DC[@]}" up -d
  else
    echo "==> building image (changed layers only) + starting"
    "${DC[@]}" up -d --build
  fi
  echo ""
  echo "  AIForge (docker) → http://localhost:8799/ui/"
  echo "  TLS verify: ${AIFORGE_LLM_SSL_VERIFY}   model: ${AIFORGE_LM_BASE_URL:-<unset>}"
  echo ""
  "${DC[@]}" ps
  exit 0
fi

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
# Global uv targeting the venv's python — `uv venv` does not install uv
# *into* the venv, so `.venv/bin/uv` would not exist on a fresh machine.
uv pip install --python .venv/bin/python -e . >/dev/null

# ── Connectivity test (--test) ────────────────────────────────────────
# Probe the CONFIGURED model endpoint with the current SSL settings and
# exit. Verifies BOTH reachability and that TLS is accepted (or relaxed)
# without booting the server. Never prints api keys.
if [[ $TEST -eq 1 ]]; then
  exec .venv/bin/python -m aiforge_core.cli.connectivity_test
fi

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
