#!/usr/bin/env bash
# run.sh — one-command boot for AIForge.
#
#   git clone … && cd AIForgeCrew && ./run.sh
#
# Needs: git + python 3.12. Everything else is a package.
#
# Nothing fetches a source and executes it — no installer piped into a shell,
# no Node tarball, no managed CPython, no browser binary from a CDN. That is
# structural, not a setting: uv and Node are Python dependencies now.
#
# Either way nothing downloads a source and executes it: uv and Node are Python
# dependencies (the `toolchain` extra), never an installer piped into a shell.
#
# Runs on the host by default (full fs/shell access); `--docker` runs the
# self-contained container instead. Storage is embedded SQLite + Markdown
# memory. Point it at a model on http://localhost:8799/ui/.
#
# Flags:
#   --port N     listen port (default 8799)
#   --host H     bind host (default 127.0.0.1)
#   --dev        uvicorn --reload
#   --docker     build + run the all-deps container (host FS at /host)
#   --skip-web   don't (re)build the web UI
#   --test       probe the configured model endpoint, then exit
#   --admin      this box is THE memory admin (exactly one per fleet); it
#                merges every machine's knowledge and serves the result back
#   --spoke      give up the admin role (how you MOVE the admin)
#   --admin-url <url>   name the admin this box syncs with
#   --admin-page open the loopback-only sync page, claim nothing
#   --group <name>      preselect the sync group (headless boxes only)
#   --reset-config      wipe the saved agent config (backed up)
#   --install-model2vec print the command that adds semantic memory
#   --with-graphify     print the command that adds the `graphify` CLI
#   --with-langfuse     bring up the self-hosted trace UI (needs Docker)
#   --stop-langfuse     stop it again (traces are ephemeral)
#   --migrate           force a re-converge of a prior install
#   --dedupe | --recompact-all | --migrate-okf | --purge-code
#                       memory maintenance, then exit
#   (--lite/--hybrid/--no-build are legacy no-ops)
#
# Settings live in `aiforge.env` — fixed, committed, identical everywhere and
# never written to by this script. Anything per-box goes in the real
# environment, which overrides the file:
#   AIFORGE_LM_BASE_URL    https://your-box:1234/v1
#   AIFORGE_CA_BUNDLE      /path/to/ca.pem     (keeps verification ON)
#   AIFORGE_ROLE=admin     on exactly one machine in a fleet
#
# ⚠️  The agent has FULL filesystem + shell access here (no sandbox).
#     Set AIFORGE_WORKSPACE_DIR=/path to clamp the chat file scope.
set -euo pipefail

# Export before spawning anything: the rate ceiling and the settings store are
# keyed on this path, and a supervisor with its own HOME otherwise puts the API
# and the runner on two different rate windows.
export AIFORGE_CONFIG_DIR="${AIFORGE_CONFIG_DIR:-$HOME/.aiforge}"

cd "$(dirname "$0")"

# ── the fixed env file ────────────────────────────────────────────────────
# ONE file, committed, identical on every box. run.sh READS it and never writes
# to it: a script that edits its own configuration means two sources of truth
# and a machine whose behaviour depends on what a previous run happened to
# append. Per-box differences come from the real environment — a systemd unit,
# `docker -e`, a shell export — which is why the environment WINS over the file
# rather than the other way round (`set -a; . file` would clobber it).
ENV_FILE="aiforge.env"

_load_env_file() {                       # KEY=VALUE only; no eval, no override
  local f="$1" line key val
  [[ -r "$f" ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"                 # a file edited on Windows (WSL)
    [[ "$line" =~ ^[[:space:]]*(#|$) ]] && continue
    key="${line%%=*}"; val="${line#*=}"
    key="${key#"${key%%[![:space:]]*}"}"; key="${key%"${key##*[![:space:]]}"}"
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    [[ -n "${!key+x}" ]] && continue      # already in the environment — it wins
    val="${val#"${val%%[![:space:]]*}"}"
    case "$val" in
      \"*\") val="${val#\"}"; val="${val%\"}" ;;
      \'*\') val="${val#\'}"; val="${val%\'}" ;;
    esac
    export "$key=$val"
  done < "$f"
}

if [[ -f "$ENV_FILE" ]]; then
  echo "==> loading env from $ENV_FILE (the environment overrides it)"
  _load_env_file "$ENV_FILE"
fi
# A leftover .env from before the fixed-file move is NOT read — silently
# honouring one would be exactly the per-box drift this replaced.
[[ -f .env ]] && echo "==> note: .env is ignored; settings come from $ENV_FILE" \
                      "and the environment. Delete it to stop this notice." >&2

# Storage is SQLite. Drop Postgres/Neo4j pointers a stale .env may still set,
# or everything run.sh spawns spams "Postgres unreachable". converge supplies
# its own PG url when it actually migrates.
if [[ "${AIFORGE_KEEP_PG:-0}" != "1" ]]; then
  unset AIFORGE_PG_URL AIFORGE_DSN AIFORGE_FORCE_PG \
        AIFORGE_NEO4J_URI NEO4J_URI AIFORGE_REQUIRE_DATA_BACKEND || true
  [[ "${AIFORGE_MEMORY_BACKEND:-}" == "neo4j" ]] && export AIFORGE_MEMORY_BACKEND=sqlite
fi

# ~/.aiforge/runtime.env is deliberately NOT sourced — that would execute any
# shell metacharacter in a value. The API parses it as plain KEY=VALUE.

export AIFORGE_LLM_SSL_VERIFY="${AIFORGE_LLM_SSL_VERIFY:-true}"
[[ -n "${AIFORGE_LLM_CA_BUNDLE:-}" ]] && export AIFORGE_LLM_CA_BUNDLE

# ── corporate CA, for the installs below ──────────────────────────────────
# net/ca.py publishes the bundle from a startup hook — i.e. after this script
# has finished installing everything — so without this the app trusted the
# operator's CA and pip/npm/git did not. Same resolution order as net/ca.py.
#
# SSL_CERT_FILE REPLACES the trust store, it does not add to it. Publishing a
# corporate-root-only file therefore fixes the internal hosts and breaks every
# public one: uv died with `invalid peer certificate: UnknownIssuer` fetching
# pypi.org, because the proxy does not re-sign pypi and the public roots were
# gone. So we publish root(s) PLUS the platform's own bundle, merged.
_system_ca_file() {
  local c
  for c in /etc/ssl/certs/ca-certificates.crt \
           /etc/pki/tls/certs/ca-bundle.crt \
           /etc/ssl/ca-bundle.pem \
           /etc/ssl/cert.pem; do
    [[ -r "$c" ]] && { printf '%s' "$c"; return 0; }
  done
  # macOS framework builds ship no readable bundle; certifi is a dependency.
  .venv/bin/python -c 'import certifi; print(certifi.where())' 2>/dev/null
}

_ca_bootstrap() {
  local ca="" v var sys merged
  for v in AIFORGE_CA_BUNDLE SSL_CERT_FILE REQUESTS_CA_BUNDLE; do
    [[ -n "${!v:-}" ]] && { ca="${!v}"; break; }
  done
  [[ -z "$ca" ]] \
    && ca="${AIFORGE_SECURITY_DIR:-${AIFORGE_CONFIG_DIR:-$HOME/.aiforge}/security}/ca/custom-ca.pem"
  [[ -r "$ca" ]] || return 0
  export AIFORGE_CA_BUNDLE="$ca"

  # Merge, and rebuild whenever either input is newer than the result.
  sys="$(_system_ca_file)"
  merged="$(dirname "$ca")/bundle-with-system.pem"
  if [[ -r "$sys" ]]; then
    if [[ ! -s "$merged" || "$ca" -nt "$merged" || "$sys" -nt "$merged" ]]; then
      if cat "$sys" "$ca" > "$merged.tmp" 2>/dev/null && mv "$merged.tmp" "$merged"; then
        chmod 644 "$merged" 2>/dev/null || true
      else
        rm -f "$merged.tmp"; merged="$ca"
        echo "==> WARN: could not merge the CA with the system bundle — public" >&2
        echo "    hosts (PyPI, npm) may fail with UnknownIssuer" >&2
      fi
    fi
  else
    merged="$ca"
    echo "==> WARN: no system CA bundle found; trusting ONLY $ca. Public hosts" >&2
    echo "    such as PyPI will fail unless your proxy re-signs them too." >&2
  fi

  for var in GIT_SSL_CAINFO CURL_CA_BUNDLE SSL_CERT_FILE REQUESTS_CA_BUNDLE \
             NODE_EXTRA_CA_CERTS; do
    [[ -z "${!var:-}" ]] && export "$var=$merged"   # never overrule the operator
  done
  export NPM_CONFIG_CAFILE="${NPM_CONFIG_CAFILE:-$merged}"
  # NOT UV_NATIVE_TLS: measured on uv 0.11.26 — SSL_CERT_FILE is honoured with
  # or without it, and setting it only prints a deprecation warning.
  echo "==> CA: $ca (+ system roots → $merged)"
}
_ca_bootstrap

# Mirror proxy vars across cases — tools read one or the other — and keep
# loopback direct so the local model endpoint never goes through a proxy.
for _p in http_proxy https_proxy no_proxy; do
  _P="$(echo "$_p" | tr '[:lower:]' '[:upper:]')"
  if [[ -n "${!_p:-}" && -z "${!_P:-}" ]]; then export "$_P=${!_p}"
  elif [[ -n "${!_P:-}" && -z "${!_p:-}" ]]; then export "$_p=${!_P}"; fi
done
if [[ -n "${http_proxy:-}${https_proxy:-}" ]]; then
  export no_proxy="${no_proxy:-127.0.0.1,localhost,::1}"
  export NO_PROXY="${NO_PROXY:-$no_proxy}"
fi
unset _p _P

# A plain ssh is safe and the deploy case runs sudo remotely, which would
# otherwise prompt every time. Dangerous remote commands still gate.
export AIFORGE_ALLOW_SSH="${AIFORGE_ALLOW_SSH:-1}"

# Memory sync is hub-and-spoke: every machine compacts locally, ONE machine
# (the admin) runs the cross-machine merge. Unset AIFORGE_ADMIN_URL means this
# box is the admin, which is also what a standalone install is.
[[ -n "${AIFORGE_ADMIN_URL:-}" ]] && export AIFORGE_ADMIN_URL
[[ -n "${AIFORGE_ROLE:-}" ]] && export AIFORGE_ROLE
_ROLE_FROM_ENV="${AIFORGE_ROLE:-}"       # before any flag touches it

PORT=8799
HOST=127.0.0.1
DEV=0
ADMIN=0
ADMIN_PAGE=0
UNADMIN=0
ADMIN_URL_SET=""
GROUP_SET=""
SKIP_WEB=0
TEST=0
MODE=lite                               # always SQLite; kept as a var for the blocks below
WITH_GRAPHIFY=0
WITH_LANGFUSE="${AIFORGE_LANGFUSE:-0}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --docker) MODE=docker ;;
    --lite|--hybrid|--no-build) : ;;                 # legacy no-ops
    --migrate) MIGRATE=1 ;;
    --dedupe) MAINT=dedupe ;;
    --recompact-all) MAINT=recompact ;;
    --migrate-okf) MAINT=migrateokf ;;
    --purge-code) MAINT=purge ;;
    --install-model2vec|--install-semantic) SHOW_MODEL2VEC=1 ;;
    --dev) DEV=1 ;;
    --admin) ADMIN=1; ADMIN_PAGE=1 ;;
    --admin-url) ADMIN_URL_SET="${2:-}"; shift ;;
    --group) GROUP_SET="${2:-}"; shift ;;
    --admin-page) ADMIN_PAGE=1 ;;
    --spoke) UNADMIN=1 ;;
    --skip-web) SKIP_WEB=1 ;;
    --test) TEST=1 ;;
    --with-graphify) WITH_GRAPHIFY=1 ;;
    --with-langfuse) WITH_LANGFUSE=1 ;;
    --stop-langfuse) STOP_LANGFUSE=1 ;;
    --reset-config) RESET_CONFIG=1 ;;
    --port) PORT="$2"; shift ;;
    --host) HOST="$2"; shift ;;
    # End-anchored, so new flags in the header are never dropped from --help.
    -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d'; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
  shift
done

# ── what may be fetched ───────────────────────────────────────────────────
# There is no offline switch, because there is nothing left for one to protect.
# What was dangerous was run.sh fetching a SOURCE and executing it — astral.sh's
# uv installer piped into a shell, a Node tarball unpacked onto PATH, uv's
# managed CPython, a browser binary off a CDN. All four are gone: uv and Node
# are ordinary wheels (the `toolchain` extra), resolved from the same index,
# lockfile, private mirror and CA as every other dependency. So there is no
# code path a setting could re-enable, and a switch would only ever have
# stopped a box from installing its own declared dependencies — which on a
# fresh clone means it cannot run at all.
#
# The one thing still refused outright: playwright's browser build comes from a
# CDN, not a package index. crawl4ai falls back to a plain fetch without it; a
# box that wants rendering points PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH at a
# chromium its package manager installed.
export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD="${PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD:-1}"
_fatal() {                               # a missing requirement, with its fix
  echo "!! $1" >&2
  [[ -n "${2:-}" ]] && echo "!! $2" >&2
  exit 1
}

# uv must never install a second interpreter, offline or not.
export UV_PYTHON_DOWNLOADS="${UV_PYTHON_DOWNLOADS:-never}"

# ── memory role (--admin / --spoke / --admin-url / --group) ───────────────
# The role is PERSISTED, not just exported: the systemd unit starts run.sh with
# no flags, and a machine that stops being the admin retires the fleet's merged
# fold — so a restart would delete it. --spoke is the way back out.

if [[ $ADMIN -eq 1 && $UNADMIN -eq 1 ]]; then
  echo "error: --admin and --spoke are opposites; pass one." >&2
  exit 2
fi
if [[ $ADMIN -eq 1 && "$MODE" == "docker" ]]; then
  echo "error: --admin has no meaning in --docker mode: the container does not" >&2
  echo "       run the memory sync loop. Run the admin on the host." >&2
  exit 2
fi
if [[ $ADMIN -eq 1 && -n "${AIFORGE_ADMIN_URL:-}" ]]; then
  # Refused, not overridden: silently promoting a spoke gives the fleet two
  # admins, both stamping `derived: mesh`.
  echo "error: --admin, but AIFORGE_ADMIN_URL=$AIFORGE_ADMIN_URL says this box is a spoke." >&2
  echo "       A machine cannot be both. To just open the sync page: ./run.sh --admin-page" >&2
  echo "       To make THIS box the admin: remove AIFORGE_ADMIN_URL from ${ENV_FILE:-.env} first." >&2
  exit 2
fi

if [[ -n "$ADMIN_URL_SET" ]]; then
  if [[ $ADMIN -eq 1 || "${AIFORGE_ROLE:-}" == "admin" ]]; then
    echo "error: --admin-url, but this box holds the admin role. A machine" >&2
    echo "       cannot be both. Run ./run.sh --spoke here first." >&2
    exit 2
  fi
  export AIFORGE_ADMIN_URL="$ADMIN_URL_SET"
  echo "  memory: spoke of $ADMIN_URL_SET for THIS run."
  echo "          To keep it, put AIFORGE_ADMIN_URL=$ADMIN_URL_SET in the environment"
  echo "          (systemd unit / docker -e) or in $ENV_FILE."
fi

if [[ -n "$GROUP_SET" ]]; then
  # The name becomes a directory component on the admin, so refuse rather than
  # repair it into some other group's name.
  if [[ ! "$GROUP_SET" =~ ^[A-Za-z0-9_-]+$ ]]; then
    echo "error: '$GROUP_SET' is not a usable group name — it takes [A-Za-z0-9_-]." >&2
    exit 2
  fi
  export AIFORGE_SYNC_GROUP="$GROUP_SET"
  echo "  memory: group $GROUP_SET for THIS run."
  echo "          To keep it, set AIFORGE_SYNC_GROUP=$GROUP_SET in the environment."
fi

if [[ $UNADMIN -eq 1 ]]; then
  unset AIFORGE_ROLE
  echo "  memory: NOT the admin for this run."
  echo "          Remove AIFORGE_ROLE=admin from the environment (and from"
  echo "          $ENV_FILE if it is there) or the next start claims it again."
fi

if [[ $ADMIN -eq 1 ]]; then
  # The flag claims the role for THIS process only. Nothing is written, so the
  # environment has to carry it — and a restart WITHOUT it is not neutral: a
  # machine that stops being the admin retires its own mesh fold
  # (okf/tiers._retire_own_mesh) and propagates that deletion to every spoke.
  # So say it loudly rather than let a reboot delete the fleet's memory.
  if [[ "${_ROLE_FROM_ENV:-}" != "admin" ]]; then
    echo "  memory: WARNING — --admin claims the role for THIS RUN ONLY." >&2
    echo "          AIFORGE_ROLE=admin is not in the environment, so a restart" >&2
    echo "          without --admin comes back as a NON-admin, which RETIRES" >&2
    echo "          this box's merged fold and tombstones it to every spoke." >&2
    echo "          Put AIFORGE_ROLE=admin in the unit/env that starts run.sh." >&2
  fi
  export AIFORGE_ROLE=admin
fi

if [[ "$MODE" != "docker" ]]; then
  if [[ "${AIFORGE_ROLE:-}" == "admin" ]]; then
    echo "  memory: ADMIN — merges every machine's knowledge and serves the result back"
    if [[ -n "${AIFORGE_ADMIN_URL:-}" ]]; then
      # The persisted role wins over the url, so this box ignores an admin it
      # looks configured to follow — usually a half-finished handover.
      echo "  memory: WARNING — AIFORGE_ADMIN_URL=$AIFORGE_ADMIN_URL is IGNORED while"
      echo "          this box holds the admin role. Moving the admin? Run"
      echo "          ./run.sh --spoke here once, then --admin on the new box."
    fi
  elif [[ -n "${AIFORGE_ADMIN_URL:-}" ]]; then
    if [[ -n "${AIFORGE_SYNC_GROUP:-}" ]]; then
      echo "  memory: spoke of $AIFORGE_ADMIN_URL, group $AIFORGE_SYNC_GROUP (pinned)"
    else
      echo "  memory: spoke of $AIFORGE_ADMIN_URL (group discovered from the admin)"
    fi
  elif [[ "${AIFORGE_ROLE:-}" == "spoke" ]]; then
    echo "  memory: WARNING — AIFORGE_ROLE=spoke but no AIFORGE_ADMIN_URL: this box"
    echo "          neither syncs nor merges. Set the url, or drop the role."
  else
    echo "  memory: standalone — merges only its own knowledge"
  fi
fi

# ── --stop-langfuse ───────────────────────────────────────────────────────
if [[ "${STOP_LANGFUSE:-0}" == "1" ]]; then
  if docker compose version >/dev/null 2>&1; then DC=(docker compose)
  else DC=(docker-compose); fi
  docker info >/dev/null 2>&1 || DC=(sudo "${DC[@]}")
  # --remove-orphans: a renamed service leaves containers holding the network
  # open ("Resource is still in use").
  "${DC[@]}" -p aiforge-langfuse --env-file "${AIFORGE_CONFIG_DIR:-$HOME/.aiforge}/langfuse.env" \
    -f scripts/compose/langfuse-compose.yml down --remove-orphans \
    && echo "==> langfuse stopped (ephemeral — traces do not persist)" \
    || echo "==> langfuse was not running (or docker unreachable)" >&2
  exit 0
fi

# ── --reset-config ────────────────────────────────────────────────────────
if [[ "${RESET_CONFIG:-0}" == "1" ]]; then
  _cfg_dir="${AIFORGE_CONFIG_DIR:-$HOME/.aiforge}"
  _cfg_file="${AIFORGE_SECURITY_DIR:-$_cfg_dir/security}/agent_config.json"
  [[ -f "$_cfg_file" ]] || _cfg_file="$_cfg_dir/agent_config.json"   # pre-move installs
  if [[ -f "$_cfg_file" ]]; then
    mv -f "$_cfg_file" "$_cfg_file.bak.$(date +%s)" 2>/dev/null \
      && echo "==> agent config reset (backed up): $_cfg_file" \
      || { rm -f "$_cfg_file"; echo "==> agent config reset: $_cfg_file"; }
  else
    echo "==> no saved agent config to reset ($_cfg_file)"
  fi
fi

# ── local access bootstrap ────────────────────────────────────────────────
# Journald/docker access, once, through a sudo that will NOT prompt. Never
# blocks startup. Opt out: AIFORGE_FIX_PERMS=0.
_ensure_access() {
  [[ "${AIFORGE_FIX_PERMS:-1}" == "0" ]] && return 0
  command -v usermod >/dev/null 2>&1 || return 0
  local u; u="$(id -un)"
  [[ "$u" == "root" ]] && return 0
  local SUDO=""
  if command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then SUDO="sudo -n"
  else return 0; fi
  local want=(adm systemd-journal)
  [[ -S /var/run/docker.sock ]] && ! docker info >/dev/null 2>&1 && want+=(docker)
  local added=() g
  for g in "${want[@]}"; do
    getent group "$g" >/dev/null 2>&1 || continue
    id -nG "$u" 2>/dev/null | tr ' ' '\n' | grep -qx "$g" && continue
    $SUDO usermod -aG "$g" "$u" 2>/dev/null && added+=("$g")
  done
  command -v loginctl >/dev/null 2>&1 && $SUDO loginctl enable-linger "$u" >/dev/null 2>&1 || true
  (( ${#added[@]} )) && echo "==> access: added '$u' to ${added[*]} — log out/in for it to take effect" >&2
  return 0
}
_ensure_access

# ── prerequisites ─────────────────────────────────────────────────────────
# run.sh installs NOTHING. It checks what is missing and prints the command for
# THIS machine, then stops. An operator who is told "deps install failed" has to
# go and find out what to type; one who is told what to type does not.
_os_kind() {
  case "$(uname -s 2>/dev/null)" in
    Darwin)                  echo "macos" ;;
    FreeBSD|OpenBSD|NetBSD)  echo "bsd" ;;
    MINGW*|MSYS*|CYGWIN*)    echo "windows" ;;
    Linux)
      if grep -qiE "microsoft|wsl" /proc/version 2>/dev/null; then echo "wsl"
      else echo "linux"; fi ;;
    *)                       echo "unknown" ;;
  esac
}
_OS="$(_os_kind)"

# The command that installs $1 on this box. Empty when there is no manager we
# can name, so the caller can fall back to a URL.
_install_hint() {                        # $1 = python | uv | node | tmux
  local what="$1"
  case "$_OS" in
    macos)
      case "$what" in
        python) echo "brew install python@3.12" ;;
        uv)     echo "brew install uv" ;;
        node)   echo "brew install node" ;;
        tmux)   echo "brew install tmux" ;;
      esac ;;
    windows)
      # Git Bash / MSYS2. winget is the one manager a stock Windows has.
      case "$what" in
        python) echo "winget install Python.Python.3.12    (or use WSL)" ;;
        uv)     echo "winget install astral-sh.uv          (or: pipx install uv)" ;;
        node)   echo "winget install OpenJS.NodeJS.LTS" ;;
        tmux)   echo "not available natively — use WSL, or MSYS2: pacman -S tmux" ;;
      esac ;;
    linux|wsl|bsd|unknown)
      if command -v apt-get >/dev/null 2>&1; then
        case "$what" in
          python) echo "sudo apt install -y python3.12 python3.12-venv" ;;
          uv)     echo "sudo apt install -y pipx && pipx install uv" ;;
          node)   echo "sudo apt install -y nodejs npm" ;;
          tmux)   echo "sudo apt install -y tmux" ;;
        esac
      elif command -v dnf >/dev/null 2>&1; then
        case "$what" in
          python) echo "sudo dnf install -y python3.12" ;;
          uv)     echo "sudo dnf install -y uv" ;;
          node)   echo "sudo dnf install -y nodejs npm" ;;
          tmux)   echo "sudo dnf install -y tmux" ;;
        esac
      elif command -v pacman >/dev/null 2>&1; then
        case "$what" in
          python) echo "sudo pacman -S --needed python" ;;
          uv)     echo "sudo pacman -S --needed uv" ;;
          node)   echo "sudo pacman -S --needed nodejs npm" ;;
          tmux)   echo "sudo pacman -S --needed tmux" ;;
        esac
      elif command -v apk >/dev/null 2>&1; then
        case "$what" in
          python) echo "sudo apk add python3" ;;
          uv)     echo "sudo apk add uv" ;;
          node)   echo "sudo apk add nodejs npm" ;;
          tmux)   echo "sudo apk add tmux" ;;
        esac
      elif command -v zypper >/dev/null 2>&1; then
        case "$what" in
          python) echo "sudo zypper install -y python312" ;;
          uv)     echo "sudo zypper install -y uv" ;;
          node)   echo "sudo zypper install -y nodejs npm" ;;
          tmux)   echo "sudo zypper install -y tmux" ;;
        esac
      elif command -v pkg >/dev/null 2>&1; then
        case "$what" in
          python) echo "sudo pkg install -y python312" ;;
          uv)     echo "sudo pkg install -y uv" ;;
          node)   echo "sudo pkg install -y node npm" ;;
          tmux)   echo "sudo pkg install -y tmux" ;;
        esac
      fi ;;
  esac
}

# Collected so the operator sees EVERY missing thing at once, rather than
# fixing one, re-running, and being told about the next.
_MISSING=()
_need() {                                # $1 = label, $2 = tool key
  local hint; hint="$(_install_hint "$2")"
  _MISSING+=("$(printf '  %-14s %s' "$1" "${hint:-install it from your package manager}")")
}

_report_missing() {
  (( ${#_MISSING[@]} )) || return 0
  echo "" >&2
  echo "!! Missing prerequisites on this $_OS box. run.sh installs nothing —" >&2
  echo "!! run these, then ./run.sh again:" >&2
  echo "" >&2
  printf '%s\n' "${_MISSING[@]}" >&2
  echo "" >&2
  exit 1
}

_ensure_tmux() {
  [[ "${AIFORGE_INSTALL_TMUX:-1}" == "0" ]] && return 0
  command -v tmux >/dev/null 2>&1 && return 0
  # Not fatal: without it the Doer's bash tool runs stateless (no cd/export
  # persistence between calls), which degrades the agent but boots fine.
  echo "==> tmux not found — the Doer's bash tool will run STATELESS." >&2
  echo "    $(_install_hint tmux)" >&2
}
_ensure_tmux

# ── node (a python dependency) ────────────────────────────────────────────
_NODE_MIN_MAJOR=18                       # vite 5 refuses to run below this

_node_ok() {
  command -v npm >/dev/null 2>&1 || return 1
  command -v node >/dev/null 2>&1 || return 1
  local maj; maj="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null)"
  [[ "$maj" =~ ^[0-9]+$ ]] && (( maj >= _NODE_MIN_MAJOR ))
}

# Node comes from the nodejs-wheel-binaries wheel — a declared dependency, so
# it arrives with `uv pip install -e '.[toolchain]'` and nothing is fetched
# here. The wheel's own bin/npm resolves '../lib/cli.js' against the wrong root
# and dies with MODULE_NOT_FOUND, so we write shims pointing node at npm-cli.js.
_ensure_node() {
  _node_ok && return 0
  [[ -x .venv/bin/python ]] || return 1
  .venv/bin/python -c "import nodejs_wheel" >/dev/null 2>&1 || return 1

  local root tool
  root="$(.venv/bin/python -c 'import nodejs_wheel, pathlib; print(pathlib.Path(nodejs_wheel.__file__).parent)' 2>/dev/null)" || return 1
  [[ -x "$root/bin/node" ]] || return 1
  ln -sf "$root/bin/node" .venv/bin/node
  for tool in npm npx; do
    printf '#!/bin/sh\nexec "%s/bin/node" "%s/lib/node_modules/npm/bin/%s-cli.js" "$@"\n' \
      "$root" "$root" "$tool" > ".venv/bin/$tool"
    chmod +x ".venv/bin/$tool"
  done
  hash -r 2>/dev/null || true
  _node_ok && echo "==> node $(node -v) + npm $(npm -v) (from the nodejs-wheel-binaries wheel)"
}

# Corporate boxes point ~/.npmrc at a mirror that may not carry every package,
# so try the configured registry first and fall back to public npm.
# Must be called with the working dir at web/.
# AIForge listens on plain HTTP; the banner must say https when the operator
# fronts it with TLS, or they copy a URL that will not connect.
_ui_scheme() {
  if   [[ -n "${AIFORGE_PUBLIC_SCHEME:-}" ]]; then printf '%s' "${AIFORGE_PUBLIC_SCHEME}"
  elif [[ -n "${AIFORGE_TLS:-}" ]]; then           printf 'https'
  else                                             printf 'http'
  fi
}

# ── --docker: the all-deps container, host FS at /host ────────────────────
if [[ "$MODE" == "docker" ]]; then
  if docker compose version >/dev/null 2>&1; then DC=(docker compose)
  elif command -v docker-compose >/dev/null 2>&1; then DC=(docker-compose)
  else
    echo "==> docker mode needs Docker + Compose, or use the native path: ./run.sh" >&2
    exit 1
  fi
  export AIFORGE_PORT="$PORT"
  [[ "${MIGRATE:-0}" == "1" ]] && export AIFORGE_MIGRATE=1
  mkdir -p "${AIFORGE_DATA_DIR:-./data}/aiforge"
  echo "==> docker mode: building the all-deps image (~2GB, first build takes minutes)…"
  "${DC[@]}" up -d --build
  echo "==> AIForge is up. UI: $(_ui_scheme)://${HOST}:${PORT}/ui/   (logs: ${DC[*]} logs -f aiforge)"
  echo "==> full host FS mounted at /host — set AIFORGE_HOST_ROOT to narrow it."
  exit 0
fi

# ── network posture ───────────────────────────────────────────────────────
# Web fetch is code-default OFF and forced ON here (SSRF-guarded) — do not
# delete that line thinking it is redundant. There is no web SEARCH tool.
# Fully offline box: AIFORGE_ALLOW_WEB_FETCH=0, or the hard-off
# AIFORGE_WEB_FETCH_DISABLE=1. Both are read in net/egress.py.
export AIFORGE_EXTERNAL_INGEST="${AIFORGE_EXTERNAL_INGEST:-0}"
export AIFORGE_DOCS_INDEX="${AIFORGE_DOCS_INDEX:-0}"
export AIFORGE_ALLOW_WEB_FETCH="${AIFORGE_ALLOW_WEB_FETCH:-1}"
export AIFORGE_BROWSER_ALLOWLIST="${AIFORGE_BROWSER_ALLOWLIST:-127.0.0.1,localhost}"
export DO_NOT_TRACK="${DO_NOT_TRACK:-1}"
export HF_HUB_DISABLE_TELEMETRY="${HF_HUB_DISABLE_TELEMETRY:-1}"
export LITELLM_TELEMETRY="${LITELLM_TELEMETRY:-False}"
# litellm fetches a model cost map from raw.githubusercontent.com on every
# import; the fallback it uses on failure ships inside the wheel anyway.
export LITELLM_LOCAL_MODEL_COST_MAP="${LITELLM_LOCAL_MODEL_COST_MAP:-True}"

# ── maintenance commands (existing venv, then exit) ───────────────────────
if [[ -n "${MAINT:-}" ]]; then
  if [[ ! -x .venv/bin/python ]]; then
    echo "==> no .venv yet — run ./run.sh once before a maintenance command" >&2
    exit 1
  fi
  case "$MAINT" in
    dedupe)     echo "==> dedupe: removing duplicate OKR nodes + chat sessions…"
                .venv/bin/python -m aiforge_core.memory.migrations --dedupe; exit $? ;;
    recompact)  echo "==> recompact-all: re-LLM every brief + rebuild (minutes)…"
                .venv/bin/python -m aiforge_core.memory.migrations --recompact-all; exit $? ;;
    migrateokf) echo "==> migrate-okf: converting memory to OKF frontmatter…"
                .venv/bin/python -m aiforge_core.memory.migrations --migrate-okf; exit $? ;;
    purge)      echo "==> purge-code: dropping code-as-learnings…"
                .venv/bin/python -m aiforge_core.memory.migrations --purge-code; exit $? ;;
  esac
fi

# ── python env ────────────────────────────────────────────────────────────
# uv may be installed but off a non-interactive PATH.
if ! command -v uv >/dev/null 2>&1; then
  for _d in "$HOME/.local/bin" "$HOME/.cargo/bin"; do
    [[ -x "$_d/uv" ]] && export PATH="$_d:$PATH" && break
  done
fi

# Pin the interpreter: left to itself uv grabs the newest python on the box,
# and on a fresh mac that is 3.14, for which scipy/numpy ship no wheels.
AIFORGE_PYTHON="${AIFORGE_PYTHON:-3.12}"

_pick_python() {
  local c
  for c in "python$AIFORGE_PYTHON" "python3.12" "python3" "python"; do
    command -v "$c" >/dev/null 2>&1 && { command -v "$c"; return 0; }
  done
  return 1
}

# Creating the venv downloads nothing — it is the local interpreter and the
# stdlib — so run.sh still does that much rather than make it a chore.
if [[ ! -d .venv ]]; then
  if ! _PY="$(_pick_python)"; then
    _need "python $AIFORGE_PYTHON" python
    _report_missing
  fi
  echo "==> creating .venv ($_PY)"
  if command -v uv >/dev/null 2>&1; then
    uv venv --python "$AIFORGE_PYTHON" .venv || uv venv .venv
  elif ! "$_PY" -m venv .venv; then
    echo "!! python -m venv failed with $_PY." >&2
    [[ "$_OS" != "macos" && "$_OS" != "windows" ]] \
      && echo "!! On Debian/Ubuntu the venv module is a separate package:" >&2 \
      && echo "!!   sudo apt install -y python3-venv" >&2
    exit 1
  fi
fi

UV="$(command -v uv 2>/dev/null || true)"
[[ -z "$UV" && -x .venv/bin/uv ]] && UV="$PWD/.venv/bin/uv"
if [[ -z "$UV" ]]; then
  _need "uv" uv
  _report_missing
fi
export UV
echo "==> uv: $UV ($("$UV" --version 2>/dev/null || echo unknown))"

# WSL /mnt/c: uv's cache and .venv are on different filesystems, so hardlinking
# fails and can leave broken venv scripts.
export UV_LINK_MODE="${UV_LINK_MODE:-copy}"

# Absolute, so job/Doer shells in another cwd still resolve `aiforge-tool`.
export PATH="$PWD/.venv/bin:$PATH"

# run.sh does NOT install. Everything is either a declared dependency of this
# project or something the operator installed — never something this script
# fetched on its own. So: check, and if it is not there, say exactly what to
# type. `uv pip install` is one command; a half-understood failure is an hour.
_venv_ready() { .venv/bin/python -c "import aiforge_core, pydantic_core" >/dev/null 2>&1; }

if ! _venv_ready; then
  echo "" >&2
  echo "!! The .venv does not have this project installed." >&2
  echo "!! run.sh installs nothing — run this once, then ./run.sh again:" >&2
  echo "" >&2
  echo "     $UV pip install --python .venv/bin/python -e '.[toolchain]'" >&2
  echo "" >&2
  echo "   Optional extras, same pattern:" >&2
  echo "     $UV pip install --python .venv/bin/python -e '.[structured,crawl,chunking]'   # richer tools" >&2
  echo "     $UV pip install --python .venv/bin/python -e '.[embed-static]'                # semantic memory" >&2
  echo "" >&2
  exit 1
fi
echo "==> deps: .venv is ready"

# ── converge a prior install (once, marker-guarded) ───────────────────────
# Detects a dockerized Postgres/Neo4j install, moves its data into SQLite/OKF
# and removes the DB infra. Portable — the whole flow is one Python module.
# Opt out: AIFORGE_AUTO_MIGRATE=0. Do NOT `systemctl stop aiforge-api` here:
# run.sh IS the service's ExecStart.
_cfgdir="${AIFORGE_CONFIG_DIR:-$HOME/.aiforge}"
if [[ "${AIFORGE_AUTO_MIGRATE:-1}" != "0" || "${MIGRATE:-0}" == "1" ]]; then
  _cvg=()
  [[ "${MIGRATE:-0}" == "1" ]] && _cvg=(--force)
  # ${arr[@]+"${arr[@]}"} expands to nothing when empty; a bare "${arr[@]}"
  # trips `set -u` on macOS bash 3.2.
  .venv/bin/python -m aiforge_core.deploy.converge ${_cvg[@]+"${_cvg[@]}"} || true
fi

# A leftover okr/ folder is the signal that this install predates OKF; once
# it is gone this never scans again. By hand: ./run.sh --migrate-okf
_okf_memdir="${AIFORGE_MEMORY_MD_DIR:-$_cfgdir/memory}"
if [[ "${AIFORGE_MIGRATE_OKF:-1}" != "0" && -x .venv/bin/python \
      && -d "$_okf_memdir/okr" ]]; then
  echo "==> legacy okr/ folder present → converging memory to OKF…"
  .venv/bin/python -m aiforge_core.memory.migrations --migrate-okf || true
fi

# ── RepoMap ───────────────────────────────────────────────────────────────
# Vendored in-tree; its grammars are a declared dependency. On import failure
# the agent falls back to the regex symbol map.
if [[ "${AIFORGE_SKIP_AIDER:-0}" != "1" ]]; then
  .venv/bin/python -c "import aiforge_core.indexing.repomap" >/dev/null 2>&1 \
    && echo "==> RepoMap ready (tree-sitter + PageRank)" \
    || echo "==> RepoMap unavailable (falling back to regex symbol map)"
fi

# ── optional extras + embed backend ───────────────────────────────────────
# Every seam has a built-in fallback. Skip with AIFORGE_SKIP_INTEGRATIONS=1.
if [[ "${AIFORGE_SKIP_INTEGRATIONS:-0}" != "1" ]]; then
  if ! .venv/bin/python -c "import instructor, crawl4ai, chonkie" >/dev/null 2>&1; then
    echo "==> integration extras absent (built-in fallbacks active). To add them:"
    echo "    $UV pip install --python .venv/bin/python -e '.[structured,crawl,chunking]'"
  fi

  # An explicit backend always wins; otherwise pick the lightest installed one.
  if [[ -n "${AIFORGE_EMBED_BACKEND:-}" ]]; then
    export AIFORGE_EMBED_BACKEND
    echo "==> embed backend: ${AIFORGE_EMBED_BACKEND} (explicit)"
  elif .venv/bin/python -c "import model2vec, sqlite_vec" >/dev/null 2>&1; then
    export AIFORGE_EMBED_BACKEND=model2vec
    echo "==> embed backend: model2vec (auto — static semantic, no torch)"
  else
    export AIFORGE_EMBED_BACKEND=hash
    echo "==> embed backend: hash (keyword). For semantic recall:"
    echo "    $UV pip install --python .venv/bin/python -e '.[embed-static]'"
  fi
  if [[ "${SHOW_MODEL2VEC:-0}" == "1" ]]; then
    echo "==> semantic memory (model2vec, ~30MB, no torch) — run this, then ./run.sh:"
    echo "    $UV pip install --python .venv/bin/python -e '.[embed-static]'"
  fi
fi

# ── graphify (--with-graphify) ─────────────────────────────────────────────
# ISOLATED install only — graphify pins its OWN pydantic, so putting it in
# .venv breaks the app's boot. Hence `uv tool`, and hence the operator runs it.
if [[ $WITH_GRAPHIFY -eq 1 ]] && ! command -v graphify >/dev/null 2>&1; then
  echo "==> graphify not installed. Install it yourself (NEVER into .venv):"
  echo "    $UV tool install graphifyy      # or: pipx install graphifyy"
fi

# ── --test ────────────────────────────────────────────────────────────────
if [[ $TEST -eq 1 ]]; then
  exec .venv/bin/python -m aiforge_core.cli.connectivity_test
fi

# ── web UI build ──────────────────────────────────────────────────────────
# `npm ci` reaches the registry, so the operator runs it. run.sh only builds
# what is already installed.
if [[ $SKIP_WEB -eq 0 ]]; then
  _ensure_node || true
  _web_stale() {
    [[ ! -d web/dist ]] || [[ -n "$(find web/src web/index.html web/package.json \
      -newer web/dist/index.html 2>/dev/null | head -1)" ]]
  }
  if ! command -v npm >/dev/null 2>&1; then
    if _web_stale; then
      echo "!! No npm, and web/dist is missing or stale — the UI will be wrong." >&2
      echo "!! Node comes with the toolchain extra:" >&2
      echo "!!   $UV pip install --python .venv/bin/python -e '.[toolchain]'" >&2
      echo "!! or install it yourself: $(_install_hint node)" >&2
    fi
  elif [[ ! -d web/node_modules ]]; then
    if _web_stale; then
      echo "!! web/node_modules is missing, so the UI cannot be built." >&2
      echo "!! run.sh does not install — run this once, then ./run.sh again:" >&2
      echo "!!   (cd web && npm ci --ignore-scripts && npm run build)" >&2
    fi
  elif _web_stale; then
    echo "==> building web UI"
    ( cd web && npm run build )
  else
    echo "==> web UI up to date (use --skip-web to skip this check)"
  fi
fi

# ── langfuse (--with-langfuse) ────────────────────────────────────────────
# Secrets are generated once into ~/.aiforge/langfuse.env and the project is
# provisioned on first boot; the app-side LANGFUSE_* env is exported after.
if [[ "$WITH_LANGFUSE" == "1" ]]; then
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
        echo "LF_ENCRYPTION_KEY=$(_rand 32)"       # must be 64 hex chars
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
    # --env-file, not the sourced shell env: `sudo docker compose` strips the
    # exported LF_* vars and postgres then boots with an EMPTY password.
    if "${DC[@]}" -p aiforge-langfuse --env-file "$_lf_env" \
         -f scripts/compose/langfuse-compose.yml up -d --quiet-pull --remove-orphans; then
      export LANGFUSE_HOST="http://127.0.0.1:${LF_PORT}"
      export LANGFUSE_PUBLIC_KEY="$LF_PUBLIC_KEY"
      export LANGFUSE_SECRET_KEY="$LF_SECRET_KEY"
      echo "    langfuse login: admin@aiforge.local / ${LF_ADMIN_PASSWORD}  (keys in $_lf_env)"
    else
      echo "==> WARN: langfuse bring-up failed — tracing stays off" >&2
    fi
  fi
fi

# ── launch ────────────────────────────────────────────────────────────────
export PATH="$PWD/.venv/bin:$PATH"

# CodeGraph: the Doer's codegraph_* calls are enforced, but the indexer is an
# npm package — so the operator installs it. Enforcement self-gates off without
# a binary or an index. Skip the notice with AIFORGE_SKIP_CODEGRAPH=1.
if [[ "${AIFORGE_SKIP_CODEGRAPH:-0}" != "1" ]]; then
  [[ -d "$HOME/.npm-global/bin" ]] && export PATH="$HOME/.npm-global/bin:$PATH"
  if ! command -v codegraph >/dev/null 2>&1 && [[ -z "${AIFORGE_CODEGRAPH_BIN:-}" ]]; then
    echo "==> codegraph not installed — the enforced codegraph_* tools stay off."
    echo "    bash scripts/install-codegraph.sh    # npm, user prefix, no sudo"
  fi
fi

# Those enforced calls only have data if an index exists. First run → init
# (~20s for 1200 files), thereafter → sync. Both in the background.
#   AIFORGE_CODEGRAPH_REPOS  comma-separated repo paths (empty = skip)
if [[ -z "${AIFORGE_CODEGRAPH_REPOS:-}" ]] && command -v codegraph >/dev/null 2>&1; then
  echo "  codegraph: installed but idle — set AIFORGE_CODEGRAPH_REPOS=\"/path/a,/path/b\" to index"
fi
_CG_BIN="${AIFORGE_CODEGRAPH_BIN:-codegraph}"
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
echo "  AIForge → $(_ui_scheme)://${HOST}:${PORT}/ui/   storage: SQLite + scoped-OKF memory"
echo "  code context: RepoMap + CodeGraph"
[[ -n "${AIFORGE_WORKSPACE_DIR:-}" ]] \
  && echo "  chat fs scope: ${AIFORGE_WORKSPACE_DIR}" \
  || echo "  chat fs scope: UNRESTRICTED (set AIFORGE_WORKSPACE_DIR to clamp)"
echo ""

( while true; do .venv/bin/python -m aiforge_core.runtime.adk_runner || true; sleep "${AIFORGE_RUNNER_POLL_SEC:-10}"; done ) &
RUNNER_PID=$!
echo "  runner: host pid $RUNNER_PID (polls every ${AIFORGE_RUNNER_POLL_SEC:-10}s)"

# Always on: with no approved peers a cycle touches no network at all.
( while true; do .venv/bin/python -m aiforge_core.memory.sync.loop || true; sleep 30; done ) &
SYNC_PID=$!
trap 'kill $RUNNER_PID $SYNC_PID 2>/dev/null' EXIT INT TERM
echo "  memory sync: host pid $SYNC_PID (peer pull every 30m)"

RELOAD=()
[[ $DEV -eq 1 ]] && RELOAD=(--reload)
# Lets the boot guard refuse a non-loopback bind with no AIFORGE_API_TOKEN.
export AIFORGE_BIND_HOST="$HOST"

ADMIN_URL="http://127.0.0.1:$PORT/admin"
if [[ $ADMIN_PAGE -eq 1 ]]; then
  echo "  admin: $ADMIN_URL  (loopback-only; tunnel with ssh -L $PORT:127.0.0.1:$PORT)"
  # Wait for the port in the background so uvicorn still runs in the foreground.
  (
    for _ in $(seq 1 60); do
      (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null && break
      sleep 1
    done
    if command -v open >/dev/null 2>&1; then open "$ADMIN_URL"
    elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$ADMIN_URL"
    fi
  ) >/dev/null 2>&1 &
fi

# `python -m uvicorn`, not the console script: on WSL over /mnt/c the wrapper
# fails with "cannot execute: required file not found".
# NOT `exec`: exec would replace this shell and take the trap with it, orphaning
# the runner and the sync loop whenever the API is stopped by PID.
.venv/bin/python -m uvicorn aiforge_core.api.api:app --host "$HOST" --port "$PORT" ${RELOAD[@]+"${RELOAD[@]}"} &
UVICORN_PID=$!
trap 'kill $UVICORN_PID $RUNNER_PID $SYNC_PID 2>/dev/null' EXIT INT TERM
wait "$UVICORN_PID"
