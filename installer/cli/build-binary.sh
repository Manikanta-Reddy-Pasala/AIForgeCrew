#!/usr/bin/env bash
# Freeze the aiforge CLI into one self-contained binary for THIS platform.
#
#   installer/cli/build-binary.sh              -> dist/cli/aiforge[.exe]
#   installer/cli/build-binary.sh --out DIR
#
# The result is the ONE installer: it carries the sandbox source, and
# `aiforge install` puts itself on PATH and builds + starts the sandbox, which
# serves the web UI. The machine it runs on needs docker (with compose) and
# nothing else — no python, no git, no node; those live in the sandbox. The
# engine is NOT imported by the client (tests/python/cli/test_cli_import_graph.py):
# it travels as source, packed by pack_source.py. ~22 MB.
#
# The BUILD machine needs python 3, git and this checkout.
#
# One lane per OS, each run on its own machine:
#   macOS    -> aiforge, universal2 where the interpreter is, ad-hoc signed
#   Linux    -> aiforge, built on the oldest glibc you intend to support
#   Windows  -> aiforge.exe (run this script from Git Bash)
#
# Everything comes from the configured index (UV_DEFAULT_INDEX, or pyproject's
# default [[tool.uv.index]]), wheels only. Nothing is fetched and executed.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
OUT="$ROOT/dist/cli"
PKG="$ROOT/packages/aiforge_cli"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --out) OUT="$2"; shift 2 ;;
    -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done

# Git Bash on Windows ships `python`, not `python3`.
PY=python3
command -v "$PY" >/dev/null || PY=python
command -v "$PY" >/dev/null || { echo "need python to build (not to run)" >&2; exit 3; }

BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT

echo "==> venv for the build only: $BUILD/venv"
"$PY" -m venv "$BUILD/venv"
# shellcheck disable=SC1091
. "$BUILD/venv/bin/activate" 2>/dev/null || . "$BUILD/venv/Scripts/activate"

# pip never reads [[tool.uv.index]]: without an explicit --index-url it would
# quietly use public PyPI. Always pass one — the env's, else pyproject's.
INDEX="${UV_DEFAULT_INDEX:-$("$PY" - "$ROOT/pyproject.toml" <<'PYEOF'
import re, sys
text = open(sys.argv[1], encoding="utf-8").read()
for block in re.findall(r"\[\[tool\.uv\.index\]\](.*?)(?=\n\[|\Z)", text, re.S):
    if re.search(r"^default\s*=\s*true", block, re.M):
        m = re.search(r'^url\s*=\s*"([^"]+)"', block, re.M)
        if m:
            print(m.group(1))
            break
PYEOF
)}"
[[ -n "$INDEX" ]] || { echo "no package index: set UV_DEFAULT_INDEX" >&2; exit 3; }
PIP_ARGS=(--disable-pip-version-check --index-url "$INDEX")

echo "==> build tools (pinned)"
# Wheels only for anything off the index — nothing is fetched and built here.
python -m pip install -q "${PIP_ARGS[@]}" --only-binary=:all: -r "$HERE/pins.txt"
echo "==> the CLI and its two runtime deps"
# NOT --only-binary here: $PKG is a local source tree, which pip must build a
# wheel from; --only-binary=:all: refuses that outright.
python -m pip install -q "${PIP_ARGS[@]}" "$PKG"

NAME="aiforge"
EXTRA=()
SEP=":"
case "$(uname -s)" in
  Darwin) EXTRA+=(--target-arch universal2) ;;
  MINGW*|MSYS*|CYGWIN*) NAME="aiforge"; SEP=";" ;;
esac

# The sandbox source the binary carries — exactly what the Dockerfile COPYs —
# so `aiforge install` builds the box (and its web UI) on a machine with
# nothing but docker. What git knows about only: no node_modules, venvs,
# builds or local secrets. See pack_source.py.
echo "==> packing the sandbox source"
PAYLOAD_DIR="$BUILD/payload"
mkdir -p "$PAYLOAD_DIR"
SRC_PATHS=(Dockerfile .dockerignore run.sh aiforge.env pyproject.toml uv.lock Makefile
           README.md LICENSE NOTICE aiforge_core packages web scripts services docker tests)
"$PY" "$HERE/pack_source.py" "$ROOT" "$PAYLOAD_DIR/sandbox-src.tar.gz" "${SRC_PATHS[@]}"

mkdir -p "$OUT"
echo "==> freezing"
python -m PyInstaller \
  --onefile --name "$NAME" --distpath "$OUT" \
  --workpath "$BUILD/work" --specpath "$BUILD" \
  --noconfirm --clean --console \
  --collect-submodules aiforge_cli \
  --add-data "$PAYLOAD_DIR/sandbox-src.tar.gz${SEP}aiforge_cli/_payload" \
  "${EXTRA[@]}" \
  "$PKG/aiforge_cli/_entry.py"

BIN="$OUT/$NAME"
[[ -f "$BIN.exe" ]] && BIN="$BIN.exe"

if [[ "$(uname -s)" == "Darwin" ]]; then
  echo "==> ad-hoc signature (Gatekeeper refuses an unsigned arm64 binary outright)"
  codesign --force --sign - "$BIN"
fi

echo "==> shell completions"
for shell in bash zsh fish powershell; do
  "$BIN" completion "$shell" > "$OUT/aiforge-completion.$shell"
done

echo "==> smoke test"
"$BIN" --version
"$BIN" help >/dev/null

ls -lh "$BIN"
echo "done: $BIN"
