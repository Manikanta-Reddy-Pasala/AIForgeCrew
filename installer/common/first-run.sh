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
  # uv fetches a managed CPython when the host has none of the right version —
  # which is the normal case on Ubuntu 22.04 (3.10) and stock macOS (3.9).
  "$UV" venv --python "$PY_VERSION" "$VENV"
  # --find-links: aiforge-memory is vendored, ships beside the app wheel, and
  # exists on no index. Everything else still resolves from PyPI.
  #
  # WITH THE EXTRAS. A bare wheel install pulls base dependencies only, and the
  # extras are not optional in practice — they are semantic recall (model2vec +
  # sqlite-vec), chunking, structured output and web crawl. Installed without
  # them the app starts, serves every one of its routes, and then degrades
  # feature by feature at call time, which reads as "some pages don't work".
  # `uv sync --all-extras` is what the repo and CI use; this is that.
  "$UV" pip install --python "$VENV/bin/python" --find-links "$APP_HOME" \
        "${WHEEL}[xlsx,structured,crawl,chunking,embed-static]"
  # Written last: a half-built venv must not look finished on the next launch.
  : > "$MARKER"
  # Any older marker is a previous version's — remove it so the directory does
  # not accumulate one file per release ever installed.
  find "$DATA_HOME" -maxdepth 1 -name '.installed-*' ! -name "$(basename "$MARKER")" \
    -delete 2>/dev/null || true
  echo "AIForge: runtime ready."
fi

exec "$VENV/bin/aiforge" "$@"
