#!/usr/bin/env bash
# run.sh — one-command boot for AIForge (deploy-anywhere).
#
#   git clone … && cd AIForgeCrew && ./run.sh
#
# Brings up the FULL AIForge stack by DEFAULT via docker compose: Postgres +
# Neo4j (graph memory) + embed + rerank sidecars + api + runner. Every memory
# component (tree-sitter symbol index, Neo4j graph, AiForgeMemory bundle,
# embeddings) is on — nothing degrades silently. Needs Docker.
# Point it at a model on the home page (http://localhost:8799/ui/).
#
# For a laptop / no-Docker box, `--lite` runs the embedded path instead:
# a single uvicorn process on SQLite tickets + SQLite memory (graph off).
#
# Flags:
#   --lite       embedded single-process mode (SQLite, no Docker, graph OFF).
#                The old zero-infra default — use when Docker isn't available.
#   --dev        (with --lite) uvicorn --reload (hot reload for development)
#   --port N     listen port (default 8799)
#   --host H     bind host (default 127.0.0.1)
#   --skip-web   (with --lite) don't (re)build the web UI
#   --docker     explicit full-stack (now the default; kept for back-compat)
#   --no-build   skip the image rebuild (just (re)start the stack)
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

# NOTE: ~/.aiforge/runtime.env (UI-persisted toggles) is intentionally NOT
# sourced here — sourcing it would execute any shell metacharacters a value
# contains. The API loads it itself with a plain KEY=VALUE parser at startup
# (no shell eval), so the toggles still survive a restart, safely.

# Secure-by-default: verification stays ON unless the env file flips it.
# Exporting (without override) makes the value visible to the app + probe.
export AIFORGE_LLM_SSL_VERIFY="${AIFORGE_LLM_SSL_VERIFY:-true}"
[[ -n "${AIFORGE_LLM_CA_BUNDLE:-}" ]] && export AIFORGE_LLM_CA_BUNDLE

PORT=8799
HOST=127.0.0.1
DEV=0
SKIP_WEB=0
TEST=0
DOCKER=1          # full stack is the DEFAULT; --lite opts out
NO_BUILD=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --lite) DOCKER=0 ;;
    --dev) DEV=1; DOCKER=0 ;;      # hot-reload is a lite/python-path concept
    --skip-web) SKIP_WEB=1 ;;
    --test) TEST=1; DOCKER=0 ;;    # model probe runs in the venv, no stack
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
    echo "==> 'docker' not found. The full stack (Postgres+Neo4j+sidecars) is" >&2
    echo "    the default and needs Docker. For a no-Docker box run the" >&2
    echo "    embedded path instead:  ./run.sh --lite" >&2
    exit 1
  fi
  if docker compose version >/dev/null 2>&1; then
    DC=(docker compose)
  elif command -v docker-compose >/dev/null 2>&1; then
    DC=(docker-compose)
  else
    echo "==> docker compose plugin not found." >&2; exit 1
  fi
  # Many hosts run the Docker daemon as root and the invoking user is NOT in
  # the 'docker' group → the socket is permission-denied and a bare
  # `docker compose` silently fails the deploy. Auto-fall back to sudo when
  # the daemon isn't reachable directly but passwordless sudo can reach it.
  # Override with AIFORGE_DOCKER_SUDO=1 (force) / =0 (never).
  case "${AIFORGE_DOCKER_SUDO:-auto}" in
    1|true|yes|on)  DC=(sudo "${DC[@]}"); echo "==> using sudo for docker (forced)";;
    0|false|no|off) : ;;
    *)
      if ! docker info >/dev/null 2>&1; then
        if command -v sudo >/dev/null 2>&1 && sudo -n docker info >/dev/null 2>&1; then
          DC=(sudo "${DC[@]}")
          echo "==> docker daemon needs elevation (user not in 'docker' group) — using sudo"
        else
          echo "==> cannot reach the Docker daemon. Either add your user to the" >&2
          echo "    'docker' group:  sudo usermod -aG docker \"\$USER\"  (then re-login)" >&2
          echo "    or enable passwordless sudo for docker, or set AIFORGE_DOCKER_SUDO=1." >&2
          exit 1
        fi
      fi
      ;;
  esac
  # All persistent data is host bind-mounts (not docker named volumes) — create
  # the dirs first so postgres/neo4j initdb into an existing, writable path.
  _data="${AIFORGE_DATA_DIR:-./data}"
  _ws="${AIFORGE_HOST_WORKSPACE:-$_data/workspace}"
  mkdir -p "$_data/postgres" "${NEO4J_DATA_DIR:-$_data/neo4j}" \
           "${NEO4J_LOGS_DIR:-$_data/neo4j-logs}" "$_data/aiforge" \
           "$_data/hf-cache" "$_ws"
  echo "==> host data dir: $_data   workspace: $_ws"

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
  echo "  data (host): $_data   workspace: $_ws"
  echo "  Neo4j → http://localhost:7474   Postgres → localhost:${PG_PORT:-5432}"
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
