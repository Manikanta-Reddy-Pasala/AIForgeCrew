#!/usr/bin/env bash
# First-run bootstrap shared by the .app and the .deb. Idempotent: after the
# first run it is three checks and an exec.
#
#   AIFORGE_APP_HOME   where the package put the wheel + the uv binary (read-only)
#   AIFORGE_DATA_HOME  where THIS USER's venv lives (writable, per-user)
#
# Why a per-user venv instead of one installed system-wide: the agent runs as
# the user who launched it and writes to their ~/.aiforge, their repos, their
# git config. A root-owned venv would still run as the user — it just could not
# be repaired, extended or upgraded by them. This keeps the package immutable
# and the runtime theirs.
set -euo pipefail

APP_HOME="${AIFORGE_APP_HOME:?AIFORGE_APP_HOME not set}"
DATA_HOME="${AIFORGE_DATA_HOME:-$HOME/.local/share/aiforge}"
VENV="$DATA_HOME/venv"
UV="$APP_HOME/uv/uv"
PY_VERSION="${AIFORGE_PYTHON_VERSION:-3.12}"

# Never download an interpreter: uv's managed CPython comes from GitHub
# releases, not a package index. The runtime is built on this machine's own
# Python 3.12 (a package-manager install), same as run.sh.
export UV_PYTHON_DOWNLOADS=never
export UV_PYTHON_PREFERENCE=only-system
# The internal Artifactory's CA lives in the OS trust store; uv ignores that
# store unless told.
export UV_SYSTEM_CERTS="${UV_SYSTEM_CERTS:-true}"

[[ -x "$UV" ]] || UV="$(command -v uv || true)"
if [[ -z "$UV" || ! -x "$UV" ]]; then
  echo "AIForge: no uv binary in the package and none on PATH — cannot build the runtime." >&2
  exit 1
fi

# The wheel the package shipped. A version bump replaces it, and the marker
# below is keyed on its name, so an upgraded package rebuilds the venv without
# the user being told to do anything.
# The APP wheel specifically — the directory also holds the vendored
# aiforge-memory wheel, and installing that one would produce a venv with a
# library in it and no application.
WHEEL="$(ls -1 "$APP_HOME"/aiforgecrew-*.whl 2>/dev/null | head -1 || true)"
if [[ -z "$WHEEL" ]]; then
  echo "AIForge: no wheel in $APP_HOME — the package is incomplete." >&2
  exit 1
fi
MARKER="$DATA_HOME/.installed-$(basename "$WHEEL")"

if [[ ! -f "$MARKER" || ! -x "$VENV/bin/aiforge" ]]; then
  echo "AIForge: preparing the runtime (first run after install — this needs the network once)…"
  mkdir -p "$DATA_HOME"
  # 0700: the venv holds the tokens' reach, not the tokens, but everything the
  # agent can do runs out of here.
  chmod 700 "$DATA_HOME" 2>/dev/null || true
  # uv reads pyproject.toml/uv.toml from the CURRENT directory. Launched from
  # inside a project (AIForgeCrew's own checkout names an estate-only index),
  # that project's index silently became the app's — so install from here.
  # User-level uv config (~/.config/uv) still applies.
  LAUNCH_DIR="$PWD"
  cd "$DATA_HOME"
  if ! "$UV" python find "$PY_VERSION" >/dev/null 2>&1; then
    echo "AIForge: needs Python $PY_VERSION on this machine, and found none." >&2
    case "$(uname -s)" in
      # python.org, not Homebrew: brew's bottles are served from ghcr.io.
      Darwin) echo "  the python.org installer: https://www.python.org/downloads/macos/" >&2 ;;
      *) if command -v apt-get >/dev/null 2>&1; then
           echo "  sudo apt install -y python$PY_VERSION" >&2
           echo "  (Ubuntu 22.04 has no $PY_VERSION in its archive: add ppa:deadsnakes/ppa first)" >&2
         elif command -v dnf >/dev/null 2>&1; then echo "  sudo dnf install -y python$PY_VERSION" >&2
         else echo "  install python $PY_VERSION from your package manager" >&2; fi ;;
    esac
    exit 1
  fi
  # An UPGRADE lands here too: the new wheel's name does not match the marker,
  # so this block runs again with a venv already in place. `uv venv` refuses to
  # touch an existing one ("A virtual environment already exists"), which turned
  # every upgrade into a dead launcher. So: create it only when it is not there,
  # rebuild it only when it is broken, and otherwise install the new wheel into
  # the venv that already works — which is also what makes an upgrade fast and
  # survivable on a flaky connection.
  if [[ ! -x "$VENV/bin/python" ]]; then
    if [[ -d "$VENV" ]]; then
      echo "AIForge: the runtime is incomplete — rebuilding it."
      "$UV" venv --clear --python "$PY_VERSION" "$VENV"
    else
      "$UV" venv --python "$PY_VERSION" "$VENV"
    fi
  else
    echo "AIForge: updating the existing runtime."
  fi
  # A portable OFFLINE bundle carries every wheel it needs; --offline then means
  # "resolve from the folder or fail loudly", which is the only honest behaviour
  # on a machine that has no route to an index in the first place.
  OFFLINE_ARGS=()
  if [[ -n "${AIFORGE_WHEEL_DIR:-}" && -d "${AIFORGE_WHEEL_DIR}" ]]; then
    OFFLINE_ARGS=(--offline --no-index --find-links "$AIFORGE_WHEEL_DIR")
    echo "AIForge: installing from the bundled wheels (no network needed)."
  fi
  # --find-links: aiforge-memory is vendored, ships beside the app wheel, and
  # exists on no index. Everything else still resolves from PyPI.
  #
  # WITH THE EXTRAS. A bare wheel install pulls base dependencies only, and the
  # extras are not optional in practice — they are semantic recall (model2vec +
  # sqlite-vec), chunking, structured output and web crawl. Installed without
  # them the app starts, serves every one of its routes, and then degrades
  # feature by feature at call time, which reads as "some pages don't work".
  # `uv sync --all-extras` is what the repo and CI use; this is that.
  # --override: exactly the versions uv.lock pins (build_payload.sh exports
  # them), so an installed app runs what CI tested, CVE floors included. Not
  # -c: the lock overrides google-adk's own starlette cap, which a constraint
  # cannot, and the install would be unsatisfiable.
  PIN_ARGS=()
  [[ -f "$APP_HOME/lock-pins.txt" ]] && PIN_ARGS=(--override "$APP_HOME/lock-pins.txt")
  # The index the package was built against (the internal Artifactory) — uv's
  # own default would be pypi.org. UV_DEFAULT_INDEX in the environment wins.
  if [[ -z "${UV_DEFAULT_INDEX:-}" && -s "$APP_HOME/index-url.txt" ]]; then
    UV_DEFAULT_INDEX="$(head -1 "$APP_HOME/index-url.txt")"
    export UV_DEFAULT_INDEX
  fi
  # --no-build: wheels only — the app and aiforge-memory ship as wheels beside
  # this script, every dependency has one on the index, and nothing is ever
  # built from a downloaded source archive.
  "$UV" pip install --python "$VENV/bin/python" --no-build ${OFFLINE_ARGS[@]+"${OFFLINE_ARGS[@]}"} \
        ${PIN_ARGS[@]+"${PIN_ARGS[@]}"} --find-links "$APP_HOME" \
        "${WHEEL}[xlsx,structured,crawl,chunking,embed-static]"
  # Written last: a half-built venv must not look finished on the next launch.
  : > "$MARKER"
  # Any older marker is a previous version's — remove it so the directory does
  # not accumulate one file per release ever installed.
  find "$DATA_HOME" -maxdepth 1 -name '.installed-*' ! -name "$(basename "$MARKER")" \
    -delete 2>/dev/null || true
  echo "AIForge: runtime ready."
  cd "$LAUNCH_DIR"
fi

exec "$VENV/bin/aiforge" "$@"
