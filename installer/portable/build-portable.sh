#!/usr/bin/env bash
# Build the PORTABLE bundle — unpack anywhere and run it. No installer, no
# admin, nothing written outside the folder.
#
#   AIForge-<ver>-<os>-portable.(zip|tar.gz)
#     AIForge.command / AIForge.sh / AIForge.cmd   ← double-click this
#     app/    wheels + uv + the bootstrap          ← read-only payload
#     data/   runtime/ and config/                 ← EVERYTHING it writes
#
# The difference from the .dmg/.msi/.deb is not the packaging, it is where the
# state goes: the installers put your memory, tickets and chat in ~/.aiforge
# and the venv in your profile, because that is what an installed app should
# do. This one keeps all of it in `data/` beside the app, so the folder IS the
# installation — copy it to a USB stick, another machine, another user's home,
# and it carries its own history with it.
#
# Two flavours:
#   (default)   ~20MB. First run downloads CPython + the dependencies.
#   --offline   ~1GB+. Carries a standalone CPython and every wheel, so the
#               first run needs NO network at all — the air-gapped case.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PAYLOAD="$REPO_ROOT/dist/installer"
VERSION="$(grep -m1 '^version' "$REPO_ROOT/pyproject.toml" | cut -d'"' -f2)"
TARGET=""
OFFLINE=0
PY_VERSION="${AIFORGE_PYTHON_VERSION:-3.12}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target)  TARGET="$2"; shift 2 ;;
    --offline) OFFLINE=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$TARGET" ]] || { echo "usage: build-portable.sh --target macos|macos-x64|linux|linux-arm64|windows [--offline]" >&2; exit 2; }

WHEEL="$(ls -1 "$PAYLOAD"/aiforgecrew-*.whl 2>/dev/null | head -1 || true)"
[[ -n "$WHEEL" ]] || { echo "no wheel — run installer/build_payload.sh first" >&2; exit 1; }
UV_SRC="$PAYLOAD/uv/$TARGET"
[[ -d "$UV_SRC" ]] || { echo "no uv for $TARGET — installer/build_payload.sh --target $TARGET" >&2; exit 1; }

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
ROOT="$STAGE/AIForge-$VERSION"
mkdir -p "$ROOT/app/uv" "$ROOT/data"

cp "$PAYLOAD"/*.whl "$ROOT/app/"
cp "$UV_SRC"/uv* "$ROOT/app/uv/"
chmod +x "$ROOT/app/uv/"* 2>/dev/null || true
cp "$REPO_ROOT/installer/common/first-run.sh"     "$ROOT/app/first-run.sh"
cp "$REPO_ROOT/installer/windows/first-run.ps1"   "$ROOT/app/first-run.ps1"
chmod +x "$ROOT/app/first-run.sh"

# ── offline: carry the interpreter and every dependency ─────────────────
if [[ "$OFFLINE" == "1" ]]; then
  UV_HOST="$PAYLOAD/uv/$( [[ "$(uname -s)" == "Darwin" ]] && echo macos || echo linux )/uv"
  [[ -x "$UV_HOST" ]] || UV_HOST="$(command -v uv)"
  echo "==> vendoring CPython $PY_VERSION for $TARGET"
  # uv installs a standalone build INTO the bundle; first-run.sh points
  # UV_PYTHON_INSTALL_DIR back at it, so nothing is fetched at run time.
  UV_PYTHON_INSTALL_DIR="$ROOT/app/python" "$UV_HOST" python install "$PY_VERSION" \
    || echo "    ! could not vendor a CPython for $TARGET — the bundle will fetch one on first run" >&2
  echo "==> vendoring the dependency wheels"
  # Downloaded for the TARGET, not this host: a mac cannot use linux manylinux
  # wheels and vice versa, and getting that wrong is a bundle that only works
  # on the machine that built it.
  case "$TARGET" in
    macos)       PLAT=(--python-platform aarch64-apple-darwin) ;;
    macos-x64)   PLAT=(--python-platform x86_64-apple-darwin) ;;
    linux)       PLAT=(--python-platform x86_64-unknown-linux-gnu) ;;
    linux-arm64) PLAT=(--python-platform aarch64-unknown-linux-gnu) ;;
    windows)     PLAT=(--python-platform x86_64-pc-windows-msvc) ;;
  esac
  "$UV_HOST" pip download 2>/dev/null --help >/dev/null || true
  "$UV_HOST" pip install --dry-run >/dev/null 2>&1 || true
  # `uv pip download` is not in every uv; `uv export` + pip download is, but the
  # simplest portable route is uv's own resolver writing wheels to a directory.
  if ! "$UV_HOST" pip download --python-version "$PY_VERSION" "${PLAT[@]}" \
        --only-binary :all: -d "$ROOT/app/wheels" \
        "${WHEEL}[xlsx,structured,crawl,chunking,embed-static]" \
        --find-links "$ROOT/app" >/dev/null 2>&1; then
    echo "    ! wheel vendoring failed (a source-only dependency, or this uv has no"
    echo "      'pip download'). The bundle still works — its first run fetches deps." >&2
    rm -rf "$ROOT/app/wheels"
  fi
fi

# ── launchers ───────────────────────────────────────────────────────────
# Each one does the same three things: point APP_HOME at app/, point DATA_HOME
# and CONFIG_DIR at data/ (this is what makes it portable), then hand off to the
# shared bootstrap.
posix_launcher() {
  cat <<'LAUNCH'
#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export AIFORGE_APP_HOME="$HERE/app"
export AIFORGE_DATA_HOME="$HERE/data/runtime"
# The whole point of "portable": memory, tickets and chat live in the folder,
# not in this machine's home directory.
export AIFORGE_CONFIG_DIR="$HERE/data/config"
# An offline bundle carries its own interpreter; uv finds it here instead of
# downloading one.
[[ -d "$HERE/app/python" ]] && export UV_PYTHON_INSTALL_DIR="$HERE/app/python"
[[ -d "$HERE/app/wheels" ]] && export AIFORGE_WHEEL_DIR="$HERE/app/wheels"
mkdir -p "$AIFORGE_DATA_HOME" "$AIFORGE_CONFIG_DIR"
exec "$HERE/app/first-run.sh" --open "$@"
LAUNCH
}

case "$TARGET" in
  macos|macos-x64)
    posix_launcher > "$ROOT/AIForge.command"; chmod +x "$ROOT/AIForge.command" ;;
  linux|linux-arm64)
    posix_launcher > "$ROOT/AIForge.sh";      chmod +x "$ROOT/AIForge.sh" ;;
  windows)
    cat > "$ROOT/AIForge.cmd" <<'CMD'
@echo off
REM Portable: everything this writes stays under data\ next to this file.
setlocal
set "HERE=%~dp0"
set "AIFORGE_APP_HOME=%HERE%app"
set "AIFORGE_DATA_HOME=%HERE%data\runtime"
set "AIFORGE_CONFIG_DIR=%HERE%data\config"
if exist "%HERE%app\python" set "UV_PYTHON_INSTALL_DIR=%HERE%app\python"
if exist "%HERE%app\wheels" set "AIFORGE_WHEEL_DIR=%HERE%app\wheels"
if not exist "%AIFORGE_DATA_HOME%" mkdir "%AIFORGE_DATA_HOME%"
if not exist "%AIFORGE_CONFIG_DIR%" mkdir "%AIFORGE_CONFIG_DIR%"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%HERE%app\first-run.ps1" --open %*
endlocal
CMD
    ;;
esac

cat > "$ROOT/README.txt" <<TXT
AIForge $VERSION — portable

  Run it:   $( [[ "$TARGET" == windows ]] && echo "AIForge.cmd" \
              || { [[ "$TARGET" == macos* ]] && echo "AIForge.command" || echo "./AIForge.sh"; } )
  Then:     http://localhost:8799/ui/

Nothing is installed and nothing is written outside this folder. Your memory,
tickets, chat history and the Python runtime all live in data/ — copy the whole
folder to another machine and it carries its history with it.

$( [[ -d "$ROOT/app/python" ]] \
   && echo "This is the OFFLINE bundle: the interpreter and dependencies are included, so the first run needs no network." \
   || echo "The FIRST run downloads a Python 3.12 and the dependencies into data/runtime — it needs the network once. Every run after that does not." )

macOS: the first launch is blocked by Gatekeeper because this is unsigned —
right-click AIForge.command -> Open, once.

To remove it: delete the folder.
TXT

# ── archive ─────────────────────────────────────────────────────────────
SUFFIX="$( [[ "$OFFLINE" == "1" ]] && echo "-offline" || echo "" )"
OUT="$PAYLOAD/AIForge-${VERSION}-${TARGET}-portable${SUFFIX}"
rm -f "$OUT.zip" "$OUT.tar.gz"
if [[ "$TARGET" == windows ]]; then
  ( cd "$STAGE" && zip -qr "$OUT.zip" "AIForge-$VERSION" ) && OUT="$OUT.zip"
else
  # tar keeps the exec bits that a zip on some tools drops — which would leave
  # a "portable" bundle whose launcher cannot be launched.
  # COPYFILE_DISABLE + --no-xattrs: a tar rolled on macOS otherwise carries
  # com.apple.provenance on every file, and GNU tar on the target prints a
  # warning per file while unpacking — pages of noise on a "portable" bundle's
  # very first impression.
  ( cd "$STAGE" && COPYFILE_DISABLE=1 tar --no-xattrs -czf "$OUT.tar.gz" "AIForge-$VERSION" \
      2>/dev/null || COPYFILE_DISABLE=1 tar -czf "$OUT.tar.gz" "AIForge-$VERSION" ) \
    && OUT="$OUT.tar.gz"
fi
echo "==> $OUT"
du -h "$OUT" | sed 's/^/    /'
