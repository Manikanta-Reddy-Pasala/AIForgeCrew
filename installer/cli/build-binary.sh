#!/usr/bin/env bash
# Freeze the aiforge CLI into one self-contained binary for THIS platform.
#
#   installer/cli/build-binary.sh              -> dist/cli/aiforge[.exe]
#   installer/cli/build-binary.sh --out DIR
#
# Why a binary: the host side of AIForge should need nothing but docker. No
# python, no git, no node — those live in the sandbox. The engine is NOT in
# here (see tests/python/cli/test_cli_import_graph.py); this is the ~15 MB
# client that starts the box and streams its events.
#
# One lane per OS, each run on its own machine:
#   macOS    -> aiforge, universal2 where the interpreter is, ad-hoc signed
#   Linux    -> aiforge, built on the oldest glibc you intend to support
#   Windows  -> aiforge.exe (run this script from Git Bash)
#
# Everything comes from the configured index (UV_DEFAULT_INDEX, or pyproject's),
# wheels only. Nothing is fetched and executed.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
OUT="$ROOT/dist/cli"
PKG="$ROOT/packages/aiforge_cli"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --out) OUT="$2"; shift 2 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
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

PIP_ARGS=(--disable-pip-version-check)
[[ -n "${UV_DEFAULT_INDEX:-}" ]] && PIP_ARGS+=(--index-url "$UV_DEFAULT_INDEX")

echo "==> build tools (pinned)"
# Wheels only for anything off the index — nothing is fetched and built here.
python -m pip install -q "${PIP_ARGS[@]}" --only-binary=:all: -r "$HERE/pins.txt"
echo "==> the CLI and its two runtime deps"
# NOT --only-binary here: $PKG is a local source tree, which pip must build a
# wheel from; --only-binary=:all: refuses that outright.
python -m pip install -q "${PIP_ARGS[@]}" "$PKG"

NAME="aiforge"
EXTRA=()
case "$(uname -s)" in
  Darwin) EXTRA+=(--target-arch universal2) ;;
  MINGW*|MSYS*|CYGWIN*) NAME="aiforge" ;;
esac

mkdir -p "$OUT"
echo "==> freezing"
python -m PyInstaller \
  --onefile --name "$NAME" --distpath "$OUT" \
  --workpath "$BUILD/work" --specpath "$BUILD" \
  --noconfirm --clean --console \
  --collect-submodules aiforge_cli \
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
