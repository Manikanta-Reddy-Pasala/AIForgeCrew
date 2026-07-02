#!/usr/bin/env bash
# run.sh — one-command boot for AIForge (deploy-anywhere).
#
#   git clone … && cd AIForgeCrew && ./run.sh
#
# THREE deployment modes (default = hybrid):
#
#   hybrid  (DEFAULT, bare ./run.sh)
#           Data + infra in Docker (Postgres + Neo4j + embed + rerank);
#           the AGENT (api + runner) runs on the HOST — so it gets the host
#           filesystem, host shell and host toolchain a coding agent needs.
#           Env is pointed at the dockerized infra over localhost.
#
#   --docker  Full stack in containers (api + runner too). Isolated: the
#             agent only sees the mounted workspace, NOT host tools. This
#             was the previous default; kept for isolated/shared boxes.
#
#   --lite    All on the host, embedded SQLite tickets + SQLite memory,
#             NO Docker, graph OFF. Use on a laptop / no-Docker box.
#
# Point it at a model on the home page (http://localhost:8799/ui/).
#
# Flags:
#   --hybrid     infra in docker, agent (api+runner) on host  [DEFAULT]
#   --docker     full stack in containers (isolated agent)
#   --lite       embedded single-process host mode (SQLite, no Docker)
#   --dev        uvicorn --reload (host hot reload; implies host, not docker)
#   --port N     listen port (default 8799)
#   --host H     bind host (default 127.0.0.1)
#   --skip-web   don't (re)build the web UI
#   --no-build   skip the docker image rebuild (just (re)start the stack)
#   --reset-config  wipe ~/.aiforge/agent_config.json (backs it up) so stale
#                per-role rows can't shadow the model you set next. Run once,
#                then reconfigure the model on the home page.
#   --test       probe the configured model endpoint with the current SSL
#                settings (OK/FAIL + error), then exit. Runs in the venv,
#                needs no Docker; works in every mode.
#
# Self-hosted model over HTTPS with an internal/self-signed cert?
# Drop an `.env` (or `aiforge.env`) next to this script — it is sourced
# automatically. See `.env.example`. Relevant keys:
#   AIFORGE_LM_BASE_URL       https://your-box:1234/v1   (the model endpoint)
#   AIFORGE_LLM_SSL_VERIFY    false   (relax TLS for INTERNAL hosts only)
#   AIFORGE_LLM_CA_BUNDLE     /path/to/ca.pem  (preferred: keep verify ON)
#
# ⚠️  In hybrid/lite the agent has FULL filesystem + shell access on this
#     machine (no sandbox). Set AIFORGE_WORKSPACE_DIR=/path to clamp it, or
#     use --docker for shared/untrusted deploys.
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
MODE=hybrid       # infra in docker, agent on host — the DEFAULT
NO_BUILD=0
DOWN_FIRST=0      # full --docker restart tears down stale containers first
while [[ $# -gt 0 ]]; do
  case "$1" in
    --lite) MODE=lite ;;
    --docker) MODE=docker ;;
    --hybrid) MODE=hybrid ;;
    --dev) DEV=1; [[ $MODE == docker ]] && MODE=hybrid ;;  # dev is a host concept
    --skip-web) SKIP_WEB=1 ;;
    --test) TEST=1 ;;             # model probe runs in the venv, no stack
    --no-build) NO_BUILD=1 ;;
    --reset-config) RESET_CONFIG=1 ;;
    --port) PORT="$2"; shift ;;
    --host) HOST="$2"; shift ;;
    -h|--help) sed -n '2,48p' "$0"; exit 0 ;;
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

# ── Docker infra bring-up (reusable) ──────────────────────────────────
# Resolves the compose command into the global DC[] array (with sudo
# auto-fallback), creates the host bind-mount data dirs, and brings up the
# services passed as args (no args = ALL services). Postgres is a named
# volume so it needs no host mkdir. Sets the global DC[] for later `ps`.
# Used by BOTH --docker (all services) and hybrid (infra only).
_docker_infra_up() {
  if ! command -v docker >/dev/null 2>&1; then
    echo "==> 'docker' not found. This mode needs Docker for Postgres+Neo4j+" >&2
    echo "    sidecars. Install Docker or use the no-Docker path:  ./run.sh --lite" >&2
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
  # the dirs first so neo4j/sidecars initdb into an existing, writable path.
  _data="${AIFORGE_DATA_DIR:-./data}"
  _ws="${AIFORGE_HOST_WORKSPACE:-$_data/workspace}"
  mkdir -p "${NEO4J_DATA_DIR:-$_data/neo4j}" \
           "${NEO4J_LOGS_DIR:-$_data/neo4j-logs}" "$_data/aiforge" \
           "$_data/hf-cache" "$_ws"
  echo "==> host data dir: $_data   workspace: $_ws"

  if [[ $DOWN_FIRST -eq 1 ]]; then
    echo "==> stopping any running AIForge containers"
    "${DC[@]}" down --remove-orphans || true
  fi
  if [[ $NO_BUILD -eq 1 ]]; then
    echo "==> starting (no rebuild): ${*:-all services}"
    "${DC[@]}" up -d "$@"
  else
    echo "==> building image (changed layers only) + starting: ${*:-all services}"
    "${DC[@]}" up -d --build "$@"
  fi
}

# ── Mode branches ─────────────────────────────────────────────────────
# --test never touches Docker (it only probes the model endpoint in the
# venv), so skip the whole docker bring-up when TEST=1 — venv setup + the
# probe happen further down, uniformly for every mode.
if [[ $TEST -eq 0 ]]; then
  case "$MODE" in
    docker)
      # Full Postgres + Neo4j + sidecars + api + runner in containers. Isolated:
      # the agent can only reach the mounted workspace. Tear down stale
      # containers first so an old build can't shadow the new one, then up ALL.
      DOWN_FIRST=1
      _docker_infra_up
      echo ""
      echo "  AIForge (docker) → http://localhost:${PORT}/ui/"
      echo "  mode: docker (full stack in containers — agent isolated from host)"
      echo "  data (host): ${AIFORGE_DATA_DIR:-./data}"
      echo "  Neo4j → http://localhost:7474   Postgres → localhost:${PG_PORT:-5432}"
      echo "  TLS verify: ${AIFORGE_LLM_SSL_VERIFY}   model: ${AIFORGE_LM_BASE_URL:-<unset>}"
      echo ""
      "${DC[@]}" ps
      exit 0
      ;;
    hybrid)
      # INFRA ONLY in docker; api + runner run on the host (fall through to the
      # host venv/web/launch path below — do NOT exit here).
      _docker_infra_up postgres neo4j embed rerank
      # `up -d` returns before the DBs accept connections. Wait (bounded) so
      # the host api/runner don't error on a cold start. Best-effort — proceed
      # after the cap regardless (the app also retries).
      echo "==> waiting for Postgres + Neo4j to accept connections…"
      for _i in $(seq 1 "${AIFORGE_INFRA_WAIT_S:-60}"); do
        _pg=0; _neo=0
        "${DC[@]}" exec -T postgres pg_isready -q >/dev/null 2>&1 && _pg=1
        { exec 3<>/dev/tcp/127.0.0.1/7687; exec 3>&-; } 2>/dev/null && _neo=1
        [[ $_pg -eq 1 && $_neo -eq 1 ]] && { echo "==> infra ready"; break; }
        sleep 1
      done
      # Point the HOST api/runner at the dockerized infra over localhost.
      # Operator env wins everywhere (`:-`).
      export AIFORGE_PG_URL="postgresql://${PG_USER:-aiforge}:${PG_PASSWORD:-aiforgepass}@127.0.0.1:${PG_PORT:-5432}/${PG_DB:-aiforge}"
      export AIFORGE_DSN="$AIFORGE_PG_URL"
      export AIFORGE_FORCE_PG=1
      export AIFORGE_NEO4J_URI="bolt://127.0.0.1:7687"; export NEO4J_URI="$AIFORGE_NEO4J_URI"
      export AIFORGE_NEO4J_USER="${AIFORGE_NEO4J_USER:-neo4j}"
      export AIFORGE_NEO4J_PASSWORD="${NEO4J_PASSWORD:-password}"
      export AIFORGE_MEMORY_BACKEND=neo4j
      export AIFORGE_EMBED_URL="http://127.0.0.1:8764"
      export AIFORGE_RERANK_URL="http://127.0.0.1:8765"
      export AIFORGE_REPO_ROOT="${AIFORGE_HOST_WORKSPACE:-$HOME/aiforge_workspace}"
      mkdir -p "$AIFORGE_REPO_ROOT"
      ;;
    lite)
      # All host, embedded SQLite, no docker infra. Nothing to bring up — just
      # fall through to the host launch (SQLite is the code default).
      :
      ;;
  esac
fi

# ── Network lockdown (hybrid + lite host processes) ───────────────────
# The compose stack runs locked-down; the HOST process must match it. The
# code default for external-ingest is ON, so run.sh forces it off here.
# Operator overrides win via `:-`.
if [[ $MODE == hybrid || $MODE == lite ]]; then
  export AIFORGE_EXTERNAL_INGEST="${AIFORGE_EXTERNAL_INGEST:-0}"
  export AIFORGE_DOCS_INDEX="${AIFORGE_DOCS_INDEX:-0}"
  export AIFORGE_ALLOW_WEB_FETCH="${AIFORGE_ALLOW_WEB_FETCH:-0}"
  export AIFORGE_BROWSER_ALLOWLIST="${AIFORGE_BROWSER_ALLOWLIST:-127.0.0.1,localhost}"
  export DO_NOT_TRACK="${DO_NOT_TRACK:-1}"
  export HF_HUB_DISABLE_TELEMETRY="${HF_HUB_DISABLE_TELEMETRY:-1}"
  export LITELLM_TELEMETRY="${LITELLM_TELEMETRY:-False}"
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
# without booting the server. Needs the venv but NO Docker → works in
# every mode. Never prints api keys.
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

# ── Launch (host: api + runner) ───────────────────────────────────────
echo ""
if [[ $MODE == hybrid ]]; then
  echo "  AIForge → http://${HOST}:${PORT}/ui/   mode: hybrid (infra docker, agent host)"
  echo "  workspace: ${AIFORGE_REPO_ROOT}"
  echo "  Postgres → ${AIFORGE_PG_URL}"
  echo "  Neo4j → ${AIFORGE_NEO4J_URI}   embed → ${AIFORGE_EMBED_URL}   rerank → ${AIFORGE_RERANK_URL}"
else
  echo "  AIForge → http://${HOST}:${PORT}/ui/   mode: lite"
  echo "  storage: SQLite (use hybrid/--docker for the Postgres+Neo4j backends)"
fi
[[ -n "${AIFORGE_WORKSPACE_DIR:-}" ]] \
  && echo "  chat fs scope: ${AIFORGE_WORKSPACE_DIR}" \
  || echo "  chat fs scope: UNRESTRICTED (set AIFORGE_WORKSPACE_DIR to clamp)"
echo ""

# hybrid needs the team pipeline runner — start it on the HOST in the
# background and reap it when uvicorn exits. lite has no runner (unchanged).
if [[ $MODE == hybrid ]]; then
  ( while true; do .venv/bin/python -m aiforge_core.runtime.adk_runner || true; sleep "${AIFORGE_RUNNER_POLL_SEC:-10}"; done ) &
  RUNNER_PID=$!
  trap 'kill $RUNNER_PID 2>/dev/null' EXIT INT TERM
  echo "  runner: host pid $RUNNER_PID (polls every ${AIFORGE_RUNNER_POLL_SEC:-10}s)"
fi

RELOAD=()
[[ $DEV -eq 1 ]] && RELOAD=(--reload)
exec .venv/bin/uvicorn aiforge_core.api.api:app --host "$HOST" --port "$PORT" "${RELOAD[@]}"
