#!/usr/bin/env bash
# run.sh — one-command boot for AIForge (deploy-anywhere).
#
#   git clone … && cd AIForgeCrew && ./run.sh
#
# ZERO PREREQS — on a clean machine run.sh installs its own toolchain:
#   • uv       — auto-installed via astral.sh if missing (needs curl or wget)
#   • Node/npm — a portable Node is fetched into ~/.aiforge/node if missing
#                (no sudo, no nvm; Linux/macOS x64+arm64). Pin: AIFORGE_NODE_VERSION
#   • python + node deps, Aider RepoMap, CodeGraph — installed on first boot
# You just need git + curl (or wget). Everything else is bootstrapped.
#
# SINGLE MODE — everything on the host, zero infra Docker:
#   • embedded SQLite  (tickets + chat)
#   • scoped-OKR Markdown memory  (briefs in ~/.aiforge/memory/compacted/,
#       originals archived to archive/; global 'shared' + per-repo + topic briefs)
#   • hybrid recall  (keyword/BM25 + spell-correct by default; add semantic
#       vector KNN with --install-semantic — optional, see below)
#   • Aider RepoMap + CodeGraph  (code context — auto-installed)
#   • api + team-pipeline runner on the host (full fs/shell/toolchain)
# A prior dockerized install (Postgres/Neo4j) is auto-migrated to SQLite/OKR on
# first boot and its DB containers/images/volumes removed (see
# aiforge_core.deploy.converge). Point it at a model on the home page
# (http://localhost:8799/ui/).
#
# MEMORY EMBEDDER (recall quality):
#   • Default = 'hash' backend — keyword + exact-id + spell-correction. Works
#     fully (briefs, migration, chat, contradiction, seed-index, lint). A normal
#     boot NEVER downloads anything heavy.
#   • Semantic = meaning/paraphrase recall (sentence-transformers + sqlite-vec,
#     pulls torch, ~minutes). Enable ONCE with `./run.sh --install-semantic`
#     (installs, then starts with semantic active); afterwards every ./run.sh
#     auto-detects it. Force hash / skip: AIFORGE_EMBED_BACKEND=hash ./run.sh
#
# SEED A FRESH MACHINE'S MEMORY from agent-instruction files (CLAUDE.md /
# AGENTS.md / GEMINI.md / .cursorrules) — a reproducible, committed path:
#   aiforge-memory-instructions --clear --root <repos-dir>   # (stop api first)
#
# Flags:
#   --port N     listen port (default 8799)
#   --host H     bind host (default 127.0.0.1)
#   --dev        uvicorn --reload (hot reload)
#   --skip-web   don't (re)build the web UI
#   --test       probe the configured model endpoint (OK/FAIL), then exit
#   --reset-config  wipe ~/.aiforge/agent_config.json (backed up) so stale
#                per-role rows can't shadow the model you set next
#   --with-langfuse  bring up a self-hosted Langfuse trace UI (the ONE optional
#                Docker piece; also AIFORGE_LANGFUSE=1 in .env)
#   --stop-langfuse  stop the langfuse containers (traces are ephemeral)
#   --with-graphify  install the `graphify` CLI (concept-graph tool)
#   --migrate    force a (re-)converge: migrate a prior PG/Neo4j install →
#                SQLite/OKR + remove docker, then start
#   --install-semantic  install the semantic-memory extra (torch; foreground,
#                shows progress), then start with semantic active. One-time.
#   --dedupe     remove duplicate OKR nodes + chat sessions, then exit
#   --recompact-all  re-LLM every memory brief + rebuild from scratch, then exit
#   --purge-code     drop code-as-learnings from a bad migration, then exit
#   (--lite/--hybrid/--docker/--no-build are legacy no-ops — always SQLite now)
#
# Self-hosted model over HTTPS with an internal/self-signed cert? Drop an `.env`
# (or `aiforge.env`) next to this script — it is sourced automatically:
#   AIFORGE_LM_BASE_URL       https://your-box:1234/v1   (the model endpoint)
#   AIFORGE_LLM_SSL_VERIFY    false   (relax TLS for INTERNAL hosts only)
#   AIFORGE_LLM_CA_BUNDLE     /path/to/ca.pem  (preferred: keep verify ON)
#
# ⚠️  The agent has FULL filesystem + shell access on this machine (no sandbox).
#     Set AIFORGE_WORKSPACE_DIR=/path to clamp the chat file scope.
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

# SINGLE MODE = embedded SQLite. Immediately drop any Postgres/Neo4j pointers a
# stale .env (from an old hybrid setup) may have set, so NOTHING run.sh spawns
# (converge, the api, the runner) tries a Postgres that no longer exists and
# spams "Postgres unreachable". converge provides its own PG url internally when
# it actually migrates. Set AIFORGE_KEEP_PG=1 only if you truly run external PG.
if [[ "${AIFORGE_KEEP_PG:-0}" != "1" ]]; then
  unset AIFORGE_PG_URL AIFORGE_DSN AIFORGE_FORCE_PG \
        AIFORGE_NEO4J_URI NEO4J_URI AIFORGE_REQUIRE_DATA_BACKEND || true
  [[ "${AIFORGE_MEMORY_BACKEND:-}" == "neo4j" ]] && export AIFORGE_MEMORY_BACKEND=sqlite
fi

# NOTE: ~/.aiforge/runtime.env (UI-persisted toggles) is intentionally NOT
# sourced here — sourcing it would execute any shell metacharacters a value
# contains. The API loads it itself with a plain KEY=VALUE parser at startup
# (no shell eval), so the toggles still survive a restart, safely.

# Secure-by-default: verification stays ON unless the env file flips it.
# Exporting (without override) makes the value visible to the app + probe.
export AIFORGE_LLM_SSL_VERIFY="${AIFORGE_LLM_SSL_VERIFY:-true}"
[[ -n "${AIFORGE_LLM_CA_BUNDLE:-}" ]] && export AIFORGE_LLM_CA_BUNDLE

# ssh deploys run free by default: a plain ssh is already safe, and an ssh whose
# REMOTE command runs sudo/systemctl (the deploy case) would otherwise prompt
# for approval every time. A DANGEROUS remote command (rm -rf / secret exfil)
# still gates, and LOCAL sudo is unaffected. Set AIFORGE_ALLOW_SSH=0 in .env to
# require approval for ssh again.
export AIFORGE_ALLOW_SSH="${AIFORGE_ALLOW_SSH:-1}"

PORT=8799
HOST=127.0.0.1
DEV=0
SKIP_WEB=0
TEST=0
# Default mode is config-driven: AIFORGE_MODE (from .env / the service env) →
# lite | hybrid | docker; a CLI flag below still overrides. DEFAULT is LITE
# (zero-Docker, embedded SQLite) — matches the deploy-anywhere direction and
# means a fresh clone never spins up Postgres/Neo4j containers unless the
# operator explicitly asks (--hybrid / --docker / AIFORGE_MODE=hybrid).
# SINGLE MODE: embedded SQLite + scoped-OKR memory + Aider RepoMap + CodeGraph,
# all on the host — no Docker infra. Kept as a var (always "lite") so the
# converge/langfuse/lockdown blocks read the same name.
MODE=lite
WITH_GRAPHIFY=0  # --with-graphify installs the graphify CLI on the host
WITH_LANGFUSE="${AIFORGE_LANGFUSE:-0}"  # --with-langfuse (or AIFORGE_LANGFUSE=1): self-hosted trace UI
while [[ $# -gt 0 ]]; do
  case "$1" in
    --lite|--hybrid|--docker|--no-build) : ;;  # legacy no-ops (always SQLite now)
    --migrate) MIGRATE=1 ;;   # force a (re-)converge: migrate PG/Neo4j → SQLite/OKR + remove docker, then start
    --dedupe) MAINT=dedupe ;;         # remove duplicate OKR nodes + chat sessions, then exit
    --recompact-all) MAINT=recompact ;;  # re-LLM every brief + rebuild, then exit
    --purge-code) MAINT=purge ;;      # drop code-as-learnings from a bad drain, then exit
    --install-semantic) INSTALL_SEMANTIC=1 ;;  # foreground-install semantic memory (torch), then continue
    --dev) DEV=1 ;;
    --skip-web) SKIP_WEB=1 ;;
    --test) TEST=1 ;;             # model probe runs in the venv, no stack
    --with-graphify) WITH_GRAPHIFY=1 ;;
    --with-langfuse) WITH_LANGFUSE=1 ;;
    --stop-langfuse) STOP_LANGFUSE=1 ;;
    --reset-config) RESET_CONFIG=1 ;;
    --port) PORT="$2"; shift ;;
    --host) HOST="$2"; shift ;;
    -h|--help) sed -n '2,60p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
  shift
done

# ── Stop the langfuse stack (--stop-langfuse) ─────────────────────────
# Tears the trace-server containers down. Data is EPHEMERAL by design — the
# v3 stack runs with NO volumes (postgres/clickhouse/redis/minio), so all
# traces are gone on stop. Exits after stopping.
if [[ "${STOP_LANGFUSE:-0}" == "1" ]]; then
  if docker compose version >/dev/null 2>&1; then DC=(docker compose)
  else DC=(docker-compose); fi
  docker info >/dev/null 2>&1 || DC=(sudo "${DC[@]}")
  # --remove-orphans: a compose change that renames/drops services (e.g. the
  # v3↔v2 swap) leaves the old containers as ORPHANS in this project, holding
  # the network open ("Resource is still in use"). Removing orphans clears them
  # + the network in one pass.
  "${DC[@]}" -p aiforge-langfuse --env-file "${AIFORGE_CONFIG_DIR:-$HOME/.aiforge}/langfuse.env" \
    -f scripts/compose/langfuse-compose.yml down --remove-orphans \
    && echo "==> langfuse stopped (ephemeral — traces do not persist)" \
    || echo "==> langfuse was not running (or docker unreachable)" >&2
  exit 0
fi

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

# ── Local access bootstrap ────────────────────────────────────────────
# A fresh host often can't read logs (journald), talk to the Docker socket, or
# use systemd-user linger — the "run as root or add user to adm/docker" wall.
# Add the CURRENT user to the groups that grant that access, once, idempotently,
# via a NON-INTERACTIVE sudo (skip silently if sudo needs a password or isn't
# there — never block startup, never prompt). Only groups that EXIST on the box
# and that the user is NOT already in are touched. New membership needs a fresh
# login to take hold in this shell; docker/converge already sudo-fall-back this
# run, so nothing is blocked meanwhile. Opt out: AIFORGE_FIX_PERMS=0.
_ensure_access() {
  [[ "${AIFORGE_FIX_PERMS:-1}" == "0" ]] && return 0
  command -v usermod >/dev/null 2>&1 || return 0          # not a Linux/usermod box
  local u; u="$(id -un)"
  [[ "$u" == "root" ]] && return 0                        # already all-access
  # sudo that will NOT prompt; if it would, bail quietly (operator can add perms)
  local SUDO=""
  if command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then SUDO="sudo -n"
  else return 0; fi
  # docker group only matters when the socket exists AND the daemon rejects us
  local want=(adm systemd-journal)
  if [[ -S /var/run/docker.sock ]] && ! docker info >/dev/null 2>&1; then
    want+=(docker)
  fi
  local added=()
  for g in "${want[@]}"; do
    getent group "$g" >/dev/null 2>&1 || continue         # group must exist
    id -nG "$u" 2>/dev/null | tr ' ' '\n' | grep -qx "$g" && continue  # already in
    $SUDO usermod -aG "$g" "$u" 2>/dev/null && added+=("$g")
  done
  # user-service linger so systemctl --user survives logout (deploy target)
  if command -v loginctl >/dev/null 2>&1; then
    $SUDO loginctl enable-linger "$u" >/dev/null 2>&1 || true
  fi
  if (( ${#added[@]} )); then
    echo "==> access: added '$u' to ${added[*]} — log out/in (or 'newgrp ${added[0]}') for it to take effect in your shell" >&2
  fi
}
_ensure_access

# Portable Node.js — if the machine has no npm, fetch a self-contained Node into
# ~/.aiforge/node (no sudo, no system package manager, no nvm) so a clean box can
# build the web UI from just `./run.sh`. Sets PATH for this run and persists the
# install for the next one. Best-effort: an unsupported OS/arch, no curl/wget, or
# no network falls through to the existing stale-bundle warning.
_ensure_node() {
  command -v npm >/dev/null 2>&1 && return 0
  local ver="${AIFORGE_NODE_VERSION:-v20.18.1}" base="$HOME/.aiforge/node"
  local os arch pkg url tmp
  if [[ -x "$base/bin/npm" ]]; then export PATH="$base/bin:$PATH"; return 0; fi
  case "$(uname -s)" in
    Linux)  os=linux ;;
    Darwin) os=darwin ;;
    *) return 0 ;;                              # Windows-native etc — skip (WSL is Linux)
  esac
  case "$(uname -m)" in
    x86_64|amd64)  arch=x64 ;;
    arm64|aarch64) arch=arm64 ;;
    *) return 0 ;;
  esac
  pkg="node-${ver}-${os}-${arch}"
  url="https://nodejs.org/dist/${ver}/${pkg}.tar.gz"
  echo "==> npm not found — fetching portable Node ${ver} (${os}-${arch}) into ${base}…"
  tmp="$(mktemp -d)"
  if command -v curl >/dev/null 2>&1; then
    curl -LsSf "$url" -o "$tmp/node.tgz" || { rm -rf "$tmp"; return 0; }
  elif command -v wget >/dev/null 2>&1; then
    wget -qO "$tmp/node.tgz" "$url" || { rm -rf "$tmp"; return 0; }
  else
    rm -rf "$tmp"; return 0
  fi
  tar -xzf "$tmp/node.tgz" -C "$tmp" || { rm -rf "$tmp"; return 0; }
  mkdir -p "$(dirname "$base")"; rm -rf "$base"; mv "$tmp/$pkg" "$base"; rm -rf "$tmp"
  [[ -x "$base/bin/npm" ]] && export PATH="$base/bin:$PATH"
}

# ── Single mode: SQLite on the host (no Docker infra to bring up) ─────────
# The app runs on embedded SQLite + the scoped-OKR memory, with Aider RepoMap +
# CodeGraph for code context. Nothing to start here — fall through to venv +
# launch. (Tracing via --with-langfuse is the only optional Docker piece.)

# ── Network lockdown (host process) ───────────────────────────────────
# External-ingest/web-fetch default ON in code; force off for a self-hosted box.
# Operator overrides win via `:-`.
export AIFORGE_EXTERNAL_INGEST="${AIFORGE_EXTERNAL_INGEST:-0}"
export AIFORGE_DOCS_INDEX="${AIFORGE_DOCS_INDEX:-0}"
export AIFORGE_ALLOW_WEB_FETCH="${AIFORGE_ALLOW_WEB_FETCH:-0}"
export AIFORGE_BROWSER_ALLOWLIST="${AIFORGE_BROWSER_ALLOWLIST:-127.0.0.1,localhost}"
export DO_NOT_TRACK="${DO_NOT_TRACK:-1}"
export HF_HUB_DISABLE_TELEMETRY="${HF_HUB_DISABLE_TELEMETRY:-1}"
export LITELLM_TELEMETRY="${LITELLM_TELEMETRY:-False}"

# ── Maintenance commands (use the existing venv, no uv/deps step, then EXIT) ─
# ./run.sh --dedupe | --recompact-all | --purge-code
if [[ -n "${MAINT:-}" ]]; then
  if [[ ! -x .venv/bin/python ]]; then
    echo "==> no .venv yet — run ./run.sh once to set it up before a maintenance command" >&2
    exit 1
  fi
  case "$MAINT" in
    dedupe)    echo "==> dedupe: removing duplicate OKR nodes + chat sessions…"
               .venv/bin/python -m aiforge_core.memory.migrations --dedupe; exit $? ;;
    recompact) echo "==> recompact-all: re-LLM every brief + rebuild (minutes)…"
               .venv/bin/python -m aiforge_core.memory.migrations --recompact-all; exit $? ;;
    purge)     echo "==> purge-code: dropping code-as-learnings…"
               .venv/bin/python -m aiforge_core.memory.migrations --purge-code; exit $? ;;
  esac
fi

# ── Python env ────────────────────────────────────────────────────────
if ! command -v uv >/dev/null 2>&1; then
  echo "==> 'uv' not found — installing (astral.sh)…"
  if command -v curl >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh || true
  elif command -v wget >/dev/null 2>&1; then
    wget -qO- https://astral.sh/uv/install.sh | sh || true
  else
    echo "==> need curl or wget to auto-install uv" >&2
  fi
  # the installer drops uv in one of these — put it on PATH for this run
  for _d in "$HOME/.local/bin" "$HOME/.cargo/bin"; do
    [[ -x "$_d/uv" ]] && export PATH="$_d:$PATH"
  done
fi
if ! command -v uv >/dev/null 2>&1; then
  echo "==> uv install failed — install manually: https://docs.astral.sh/uv/  (curl -LsSf https://astral.sh/uv/install.sh | sh)" >&2
  exit 1
fi

# On WSL when the repo lives on /mnt/c (DrvFs), uv's cache (Linux ~/.cache) and
# the target .venv are on different filesystems, so hardlinking fails noisily
# and can leave broken venv scripts. Force copy mode for a portable venv.
export UV_LINK_MODE="${UV_LINK_MODE:-copy}"

if [[ ! -d .venv ]]; then
  echo "==> creating .venv"
  uv venv .venv
fi
echo "==> installing python deps (editable)"
# Global uv targeting the venv's python — `uv venv` does not install uv
# *into* the venv, so `.venv/bin/uv` would not exist on a fresh machine.
#
# On WSL /mnt/c (DrvFs) a copy can leave a package half-written — e.g. a
# `numpy-*.dist-info/` dir with NO METADATA file — and uv then aborts the
# whole resolve with "Failed to read metadata from installed package …:
# No such file or directory". A corrupt existing venv can't be patched in
# place, so on ANY install failure we nuke and rebuild it from scratch.
if ! uv pip install --python .venv/bin/python -e . >/dev/null 2>&1; then
  echo "==> deps install failed — rebuilding .venv from scratch (corrupt/partial venv; common on WSL /mnt/c)"
  rm -rf .venv && uv venv .venv
  uv pip install --python .venv/bin/python -e . >/dev/null
fi

# POST-install SMOKE IMPORT: on WSL /mnt/c (DrvFs) a copy can leave a package
# HALF-WRITTEN even though the install exits 0 — e.g. urllib3 with no
# urllib3/util dir → "ModuleNotFoundError: No module named 'urllib3.util'" at
# runtime. Import the fragile core deps; on failure, force-reinstall them, and
# if STILL broken, nuke + rebuild the whole venv. Skip with AIFORGE_SKIP_SMOKE=1.
if [[ "${AIFORGE_SKIP_SMOKE:-0}" != "1" ]]; then
  _smoke='import urllib3.util, urllib3.util.connection, requests, charset_normalizer, certifi, idna, google.adk'
  if ! .venv/bin/python -c "$_smoke" >/dev/null 2>&1; then
    echo "==> core deps import broken (partial install — common on WSL /mnt/c) — repairing…"
    uv pip install --python .venv/bin/python --reinstall \
      urllib3 requests charset_normalizer certifi idna >/dev/null 2>&1 || true
    if ! .venv/bin/python -c "$_smoke" >/dev/null 2>&1; then
      echo "==> still broken — rebuilding .venv from scratch"
      rm -rf .venv && uv venv .venv
      uv pip install --python .venv/bin/python -e . >/dev/null 2>&1 || true
    fi
    if .venv/bin/python -c "$_smoke" >/dev/null 2>&1; then
      echo "==> deps repaired"
    else
      echo "==> WARN: deps still broken. If on /mnt/c, move the repo to the Linux FS (e.g. ~/AIForgeCrew) — DrvFs corrupts venvs." >&2
    fi
  fi
fi

# ── AUTO-DETECT + converge to latest (SQLite, no infra Docker) ────────────
# On ANY invocation, identify a PRIOR install and migrate it to the current
# architecture ONCE, so an upgrade "just works" without the operator knowing to
# pass --migrate: if a dockerized Postgres with data is present, move its chat +
# tickets into the SQLite stores and remove the DB infra containers (neo4j/embed/
# rerank/postgres — volumes kept, recoverable), then run in --lite. The memory
# side (flat md / old Neo4j → scoped OKR) migrates on API startup separately
# (aiforge_core.memory.migrations). Marker-guarded (runs once); opt out with
# AIFORGE_AUTO_MIGRATE=0.
# The whole detect → migrate (PG→SQLite, Neo4j→OKR) → verify → remove DB-infra
# (containers/images/volumes, KEEP langfuse) flow lives in ONE PORTABLE Python
# module (aiforge_core.deploy.converge) so it runs the same on Linux/macOS/WSL/
# Windows — run.sh just invokes it. Marker-guarded; opt out AIFORGE_AUTO_MIGRATE=0.
_cfgdir="${AIFORGE_CONFIG_DIR:-$HOME/.aiforge}"
_automig_marker="$_cfgdir/.data_migrated_v1"
# Default: auto-converge ONCE (marker-guarded). `./run.sh --migrate` forces a
# (re-)converge now, then continues to start. Opt out entirely: AIFORGE_AUTO_MIGRATE=0.
if [[ "${AIFORGE_AUTO_MIGRATE:-1}" != "0" || "${MIGRATE:-0}" == "1" ]]; then
  # NOTE: do NOT 'systemctl stop aiforge-api' here — run.sh IS the service's
  # ExecStart, so that would kill this very process. systemd already stopped the
  # previous instance before starting us, so the SQLite files are free to migrate.
  _cvg=()
  [[ "${MIGRATE:-0}" == "1" ]] && _cvg=(--force)
  .venv/bin/python -m aiforge_core.deploy.converge "${_cvg[@]}" || true
fi

# (PG/Neo4j env was already stripped right after .env load; docker cleanup —
# stopping/removing leftover DB-infra — is handled inside the
# converge module above, portably; nothing to do here.)

# ── Aider RepoMap (optional but preferred) ────────────────────────────────
# The chat/doer repo context uses Aider's tree-sitter + PageRank RepoMap for a
# RANKED symbol map. Install best-effort — if it fails/absent the agent falls
# back to the built-in regex symbol map (aiforge_core/runtime/chat_agent.py).
# Skip with AIFORGE_SKIP_AIDER=1.
if [[ "${AIFORGE_SKIP_AIDER:-0}" != "1" ]]; then
  if ! .venv/bin/python -c "import aider.repomap" >/dev/null 2>&1; then
    echo "==> installing Aider RepoMap (ranked symbol map)…"
    uv pip install --python .venv/bin/python aider-chat >/dev/null 2>&1 \
      && echo "==> aider RepoMap ready" \
      || echo "==> aider install skipped (falling back to regex symbol map)"
  fi
fi

# ── Integration adapters (instructor / crawl4ai) ─────────────────────
# Optional extras behind aiforge_core/integrations/: instructor = validated
# structured LLM output (architect/grader/steering seams), crawl4ai =
# browser-rendered markdown for web_crawl dossiers. Best-effort — every seam
# has a built-in fallback, the stack boots without them. Skip with
# AIFORGE_SKIP_INTEGRATIONS=1.
if [[ "${AIFORGE_SKIP_INTEGRATIONS:-0}" != "1" ]]; then
  if ! .venv/bin/python -c "import instructor, crawl4ai, chonkie" >/dev/null 2>&1; then
    echo "==> installing integration extras (instructor + crawl4ai + chonkie)…"
    uv pip install --python .venv/bin/python -e '.[structured,crawl,chunking]' >/dev/null 2>&1 \
      && echo "==> integration extras ready" \
      || echo "==> integration extras skipped (built-in fallbacks active)"
  fi
  # LOCAL semantic memory (sentence-transformer embedder + sqlite-vec ANN).
  # The '.[semantic]' extra pulls torch (~hundreds of MB). Installing it during a
  # normal boot is what made run.sh look hung — inline it blocks with no output,
  # and backgrounding it holds the uv/.venv lock so the NEXT uv step blocks too.
  # So a normal boot NEVER installs it: use semantic if the libs are already
  # importable, else boot on hash and print how to enable it. Run it explicitly,
  # in the FOREGROUND with visible progress, via `./run.sh --install-semantic`.
  if [[ "${AIFORGE_EMBED_BACKEND:-}" != "hash" ]]; then
    if [[ "${INSTALL_SEMANTIC:-0}" == "1" ]] \
        && ! .venv/bin/python -c "import sentence_transformers, sqlite_vec" >/dev/null 2>&1; then
      echo "==> installing semantic memory (sentence-transformers + sqlite-vec; pulls torch, may take several minutes)…"
      if uv pip install --python .venv/bin/python -e '.[semantic]'; then
        echo "==> semantic memory installed — pre-downloading the embed model (one-time)…"
        # Warm the model cache NOW (foreground, visible) so it isn't fetched
        # lazily on the first chat message. A blocked huggingface.co fails fast
        # here (AIFORGE_EMBED_DOWNLOAD_TIMEOUT) instead of hanging a live turn.
        AIFORGE_EMBED_BACKEND=semantic .venv/bin/python -c \
          "from aiforge_core.integrations import semantic_embed as s; print('embed model ready, dim', s.dim())" \
          || echo "==> WARN: embed model could not download (box may not reach huggingface.co) — pre-download it or run AIFORGE_EMBED_BACKEND=hash"
      else
        echo "==> semantic memory install failed — continuing on hash backend"
      fi
    fi
    if .venv/bin/python -c "import sentence_transformers, sqlite_vec" >/dev/null 2>&1; then
      export AIFORGE_EMBED_BACKEND=semantic   # inherited by the api/runner below
    else
      export AIFORGE_EMBED_BACKEND=hash       # app raises on a missing semantic backend; be explicit
      echo "==> semantic memory not installed — booting on HASH backend."
      echo "==> to enable real semantic recall: ./run.sh --install-semantic (one-time; downloads torch)"
    fi
  fi
  # crawl4ai renders with headless chromium — install best-effort (idempotent).
  .venv/bin/python -m playwright install chromium >/dev/null 2>&1 || true
  # crawl4ai's deps pull urllib3/chardet versions newer than an older
  # requests' hardcoded compat check → noisy RequestsDependencyWarning on
  # EVERY python spawn. Newer requests widened the check — upgrade
  # best-effort (cosmetic; nothing breaks either way).
  uv pip install --python .venv/bin/python -U requests >/dev/null 2>&1 || true
fi

# ── venv self-heal ────────────────────────────────────────────────────
# A partial/interrupted install can leave the venv importable-but-broken —
# classic symptom: pydantic is present but its compiled companion
# `pydantic_core` wheel is not, so the API dies at boot with
# "ModuleNotFoundError: No module named 'pydantic_core'". uv then considers
# the env "satisfied", so a plain re-run won't fix it. Probe a core import;
# if it fails, force-reinstall, and rebuild the venv from scratch as a last
# resort — so `./run.sh` alone always recovers.
if ! .venv/bin/python -c "import pydantic_core" >/dev/null 2>&1; then
  echo "==> venv incomplete (pydantic_core missing) — repairing deps"
  uv pip install --python .venv/bin/python --reinstall -e . >/dev/null 2>&1 || true
  if ! .venv/bin/python -c "import pydantic_core" >/dev/null 2>&1; then
    echo "==> rebuilding .venv from scratch"
    rm -rf .venv && uv venv .venv && uv pip install --python .venv/bin/python -e . >/dev/null
  fi
fi

# ── graphify CLI (optional, --with-graphify) ──────────────────────────
# Installs the host `graphify` binary (PyPI package: graphifyy) used by the
# concept-graph refresh (docs/TOOLS.md → Graphify graph; aiforge-graphify-all.timer)
# and the graphify_lookup tool. Opt-in — the stack boots fine without it.
#
# ISOLATED install ONLY (uv tool). graphify carries a large, independently
# pinned dep set (incl. its OWN pydantic) — co-installing it into the app
# .venv clobbers the app's pinned pydantic/pydantic-core and breaks boot with
# "ModuleNotFoundError: pydantic_core". So we NEVER touch .venv here; on
# failure we warn and continue rather than fall back into the venv.
if [[ $WITH_GRAPHIFY -eq 1 ]]; then
  if command -v graphify >/dev/null 2>&1; then
    echo "==> graphify present ($(command -v graphify)) — upgrading"
    uv tool upgrade graphifyy 2>/dev/null || uv tool install --force graphifyy 2>/dev/null || true
  elif uv tool install graphifyy; then
    echo "==> graphify ready: $(command -v graphify 2>/dev/null || echo "$(uv tool dir --bin 2>/dev/null)/graphify")"
  else
    echo "==> WARN: 'uv tool install graphifyy' failed — skipping graphify (stack still boots)." \
         "Install it yourself with:  uv tool install graphifyy   (or: pipx install graphifyy)." \
         "Deliberately NOT installing into .venv — that would break the app's pydantic." >&2
  fi
fi

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
  _ensure_node                       # fetch a portable Node if the box has no npm
  if command -v npm >/dev/null 2>&1; then
    # Rebuild only when dist is missing or any source is newer than it.
    if [[ ! -d web/dist ]] || [[ -n "$(find web/src web/index.html web/package.json -newer web/dist/index.html 2>/dev/null | head -1)" ]]; then
      echo "==> building web UI"
      ( cd web && { [[ -d node_modules ]] || npm ci; } && npm run build )
    else
      echo "==> web UI up to date (use --skip-web to skip this check)"
    fi
  elif [[ ! -d web/dist ]]; then
    echo "!! npm not found and portable Node auto-install failed (no network or" >&2
    echo "!! unsupported OS/arch) AND no web/dist — the UI will not load. Install" >&2
    echo "!! Node (https://nodejs.org), then re-run; or build web/ elsewhere." >&2
  elif [[ -n "$(find web/src web/index.html web/package.json -newer web/dist/index.html 2>/dev/null | head -1)" ]]; then
    # Loud: a git pull updated the source but npm is missing, so the OLD bundle
    # is still being served — the #1 cause of "I pulled but the UI is unchanged".
    echo "!! ============================================================" >&2
    echo "!! npm not found (portable Node auto-install failed) — web/dist is STALE." >&2
    echo "!! You are serving an OUTDATED UI: source changed but the bundle was NOT" >&2
    echo "!! rebuilt. Install Node (https://nodejs.org) and re-run, or run" >&2
    echo "!! 'cd web && npm run build' on a machine with npm and copy web/dist over." >&2
    echo "!! ============================================================" >&2
  else
    echo "==> npm not found — skipping UI build (dist present + current)" >&2
  fi
fi

# ── Langfuse trace server (--with-langfuse / AIFORGE_LANGFUSE=1) ─────
# Self-hosted Langfuse v2 MINIMAL (app + postgres + hourly retention-prune
# sidecar; deliberately NOT v3's clickhouse/redis/minio/worker stack) via
# scripts/compose/langfuse-compose.yml. FULLY headless: secrets + API keys
# are generated ONCE into ~/.aiforge/langfuse.env (never committed) and the
# project is provisioned on first boot via LANGFUSE_INIT_* — then the app's
# LANGFUSE_* env is exported here so every LLM call mirrors automatically.
if [[ "$WITH_LANGFUSE" == "1" ]]; then
  # Tracing is allowed even in --lite: lite means no DB infra in Docker, but
  # langfuse (the ONLY container an operator may want) can still run when Docker
  # is present. Only skip when Docker isn't installed at all.
  if ! command -v docker >/dev/null 2>&1; then
    echo "==> --with-langfuse needs Docker (not installed) — skipped" >&2
  else
    _lf_env="${AIFORGE_CONFIG_DIR:-$HOME/.aiforge}/langfuse.env"
    if [[ ! -f "$_lf_env" ]]; then
      echo "==> generating langfuse secrets (once) → $_lf_env"
      mkdir -p "$(dirname "$_lf_env")"
      _rand() { openssl rand -hex "${1:-16}" 2>/dev/null || head -c 64 /dev/urandom | od -An -tx1 | tr -d ' \n' | cut -c1-"$(( ${1:-16} * 2 ))"; }
      {
        echo "LF_PORT=${AIFORGE_LANGFUSE_PORT:-3005}"
        echo "LF_PG_PASSWORD=$(_rand 12)"
        echo "LF_CLICKHOUSE_PASSWORD=$(_rand 12)"
        echo "LF_MINIO_PASSWORD=$(_rand 12)"
        echo "LF_NEXTAUTH_SECRET=$(_rand 24)"
        echo "LF_SALT=$(_rand 24)"
        echo "LF_ENCRYPTION_KEY=$(_rand 32)"   # 64 hex chars, required length
        echo "LF_PUBLIC_KEY=pk-lf-$(_rand 16)"
        echo "LF_SECRET_KEY=sk-lf-$(_rand 16)"
        echo "LF_ADMIN_PASSWORD=$(_rand 8)"
      } > "$_lf_env"
      chmod 600 "$_lf_env"
    fi
    set -a; . "$_lf_env"; set +a
    if [[ -z "${DC[*]:-}" ]]; then
      if docker compose version >/dev/null 2>&1; then DC=(docker compose)
      else DC=(docker-compose); fi
      docker info >/dev/null 2>&1 || DC=(sudo "${DC[@]}")
    fi
    echo "==> starting langfuse (trace UI) on http://localhost:${LF_PORT}"
    # --env-file, NOT the sourced shell env: on hosts where docker needs
    # sudo, `sudo docker compose` strips the exported LF_* vars and postgres
    # boots with an EMPTY password → unhealthy → whole stack aborts.
    if "${DC[@]}" -p aiforge-langfuse --env-file "$_lf_env" \
         -f scripts/compose/langfuse-compose.yml up -d --quiet-pull --remove-orphans; then
      # Export the app-side mirror config; tracing turns on automatically.
      export LANGFUSE_HOST="http://127.0.0.1:${LF_PORT}"
      export LANGFUSE_PUBLIC_KEY="$LF_PUBLIC_KEY"
      export LANGFUSE_SECRET_KEY="$LF_SECRET_KEY"
      echo "    langfuse login: admin@aiforge.local / ${LF_ADMIN_PASSWORD}  (keys in $_lf_env)"
    else
      echo "==> WARN: langfuse bring-up failed — tracing stays off (stack boots fine)" >&2
    fi
  fi
fi

# ── Launch (host: api + runner) ───────────────────────────────────────
# Put the venv's bin on PATH for the API, the runner, AND every subprocess
# they spawn — job/workflow scripts call the `aiforge-tool` console script
# (configured jira/confluence/gitlab access) and must find it without
# knowing the venv location.
export PATH="$PWD/.venv/bin:$PATH"

# ── CodeGraph binary — best-effort auto-install ───────────────────────────
# The Doer's codegraph_* tool calls are ENFORCED, so install the indexer if it's
# missing (npm package @colbymchenry/codegraph, user prefix, no sudo). Skip with
# AIFORGE_SKIP_CODEGRAPH=1. npm-user-global bin is put on PATH so the index block
# below resolves it. Best-effort — a box with no npm just skips (enforcement
# self-gates off when no binary/index).
if [[ "${AIFORGE_SKIP_CODEGRAPH:-0}" != "1" ]]; then
  [[ -d "$HOME/.npm-global/bin" ]] && export PATH="$HOME/.npm-global/bin:$PATH"
  if ! command -v codegraph >/dev/null 2>&1 && [[ -z "${AIFORGE_CODEGRAPH_BIN:-}" ]] \
       && command -v npm >/dev/null 2>&1; then
    echo "==> installing CodeGraph (code-graph indexer)…"
    bash scripts/install-codegraph.sh >/dev/null 2>&1 \
      && echo "==> codegraph ready" \
      || echo "==> codegraph install skipped (npm/network) — enforcement stays off"
    [[ -d "$HOME/.npm-global/bin" ]] && export PATH="$HOME/.npm-global/bin:$PATH"
  fi
fi

# ── CodeGraph index (feeds the ENFORCED codegraph_* tool calls) ───────────
# The Doer is required to call codegraph (callers/impact/explore) before
# editing an existing symbol — see runtime/text_doer._CODEGRAPH_MANDATE. Those
# calls only have data if an index exists, so build/refresh it here. Fully
# config-driven + portable: a generic clone with no codegraph binary or no
# repos configured SKIPS this cleanly.
#   AIFORGE_CODEGRAPH_BIN    codegraph binary (else `codegraph` on PATH)
#   AIFORGE_CODEGRAPH_REPOS  comma-separated repo paths to index (empty = skip)
# First run → `init` (full, ~20s for 1200 files); thereafter → `sync`
# (incremental). Both run in the background so boot is never blocked.
# No repos set but the binary is here → hint the operator (indexing is opt-in).
if [[ -z "${AIFORGE_CODEGRAPH_REPOS:-}" ]] && command -v codegraph >/dev/null 2>&1; then
  echo "  codegraph: installed but idle — set AIFORGE_CODEGRAPH_REPOS=\"/path/a,/path/b\" to index"
fi
_CG_BIN="${AIFORGE_CODEGRAPH_BIN:-codegraph}"
# Accept a bare command name (resolve via PATH) as well as an absolute path —
# mirrors AIFORGE_LMS_BIN conventions; otherwise `-x` on a bare name fails.
[[ "$_CG_BIN" != */* ]] && _CG_BIN="$(command -v "$_CG_BIN" 2>/dev/null || true)"
if [[ -n "$_CG_BIN" && -x "$_CG_BIN" && -n "${AIFORGE_CODEGRAPH_REPOS:-}" ]]; then
  IFS=',' read -ra _CG_REPOS <<< "$AIFORGE_CODEGRAPH_REPOS"
  for _r in "${_CG_REPOS[@]}"; do
    _r="$(echo "$_r" | xargs)"; [[ -d "$_r" ]] || continue
    if [[ -d "$_r/.codegraph" ]]; then
      ( "$_CG_BIN" sync "$_r" >/dev/null 2>&1 & )
      echo "  codegraph: sync $_r (incremental, background)"
    else
      ( "$_CG_BIN" init "$_r" >/dev/null 2>&1 & )
      echo "  codegraph: init $_r (first full index, background ~20s)"
    fi
  done
fi

echo ""
echo "  AIForge → http://${HOST}:${PORT}/ui/   storage: SQLite + scoped-OKR memory"
echo "  code context: Aider RepoMap + CodeGraph"
[[ -n "${AIFORGE_WORKSPACE_DIR:-}" ]] \
  && echo "  chat fs scope: ${AIFORGE_WORKSPACE_DIR}" \
  || echo "  chat fs scope: UNRESTRICTED (set AIFORGE_WORKSPACE_DIR to clamp)"
echo ""

# The team pipeline runner claims + processes tickets on the HOST. The SQLite
# claim (conditional UPDATE under the single-writer lock) is atomic. Reaped when
# uvicorn exits.
( while true; do .venv/bin/python -m aiforge_core.runtime.adk_runner || true; sleep "${AIFORGE_RUNNER_POLL_SEC:-10}"; done ) &
RUNNER_PID=$!
trap 'kill $RUNNER_PID 2>/dev/null' EXIT INT TERM
echo "  runner: host pid $RUNNER_PID (polls every ${AIFORGE_RUNNER_POLL_SEC:-10}s)"

RELOAD=()
[[ $DEV -eq 1 ]] && RELOAD=(--reload)
# Invoke uvicorn via `python -m`, NOT the .venv/bin/uvicorn console-script:
# on WSL over /mnt/c (DrvFs) the wrapper script fails with "cannot execute:
# required file not found" (exec-bit / shebang quirks). The python binary +
# module form is portable across WSL and macOS.
# Tell the app which host it's bound to, so the security boot-guard can refuse
# a non-loopback bind that has no AIFORGE_API_TOKEN set (unauth shell API on LAN).
export AIFORGE_BIND_HOST="$HOST"
exec .venv/bin/python -m uvicorn aiforge_core.api.api:app --host "$HOST" --port "$PORT" "${RELOAD[@]}"
