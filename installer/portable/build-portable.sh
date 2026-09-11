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
#   (default)   ~20MB. First run installs the locked dependencies from the index.
#   --offline   ~1GB. Carries every locked wheel, so the first run needs NO
#               network — the air-gapped case. Build it on the target's OS.
# Either way the machine needs Python 3.12: no interpreter is downloaded.
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

cp "$PAYLOAD"/*.whl "$PAYLOAD"/lock-pins.txt "$ROOT/app/"
cp "$UV_SRC"/uv* "$ROOT/app/uv/"
chmod +x "$ROOT/app/uv/"* 2>/dev/null || true
cp "$REPO_ROOT/installer/common/first-run.sh"     "$ROOT/app/first-run.sh"
cp "$REPO_ROOT/installer/windows/first-run.ps1"   "$ROOT/app/first-run.ps1"
chmod +x "$ROOT/app/first-run.sh"

# ── offline: carry every dependency ─────────────────────────────────────
# The locked versions (lock-pins.txt), as wheels for the TARGET platform, from
# the package index. No interpreter is carried: uv's managed CPython comes from
# GitHub, not an index, so the target needs its own Python 3.12.
if [[ "$OFFLINE" == "1" ]]; then
  # pip evaluates environment markers (sys_platform == 'darwin' …) against the
  # machine it runs on, so a cross-OS offline bundle would silently miss the
  # target's platform-only packages. Build it on the target's OS.
  case "$TARGET:$(uname -s)" in
    linux*:Linux|macos*:Darwin) ;;
    *) echo "build-portable: --offline for $TARGET must be built on that OS (markers are host-evaluated)" >&2; exit 2 ;;
  esac
  case "$TARGET" in
    macos)       PLAT=(--platform macosx_11_0_arm64 --platform macosx_12_0_arm64 --platform macosx_14_0_arm64) ;;
    macos-x64)   PLAT=(--platform macosx_10_12_x86_64 --platform macosx_10_13_x86_64 --platform macosx_11_0_x86_64) ;;
    linux)       PLAT=(--platform manylinux2014_x86_64 --platform manylinux_2_17_x86_64 --platform manylinux_2_28_x86_64) ;;
    linux-arm64) PLAT=(--platform manylinux2014_aarch64 --platform manylinux_2_17_aarch64 --platform manylinux_2_28_aarch64) ;;
  esac
  echo "==> vendoring the locked dependency wheels for $TARGET"
  "${PYTHON:-python3}" -m pip download --quiet --disable-pip-version-check --no-deps \
      --only-binary=:all: --python-version "$PY_VERSION" --implementation cp "${PLAT[@]}" \
      -r "$ROOT/app/lock-pins.txt" -d "$ROOT/app/wheels" \
    || { echo "build-portable: could not vendor every locked wheel for $TARGET" >&2; exit 1; }
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
[[ -d "$HERE/app/wheels" ]] && export AIFORGE_WHEEL_DIR="$HERE/app/wheels"
mkdir -p "$AIFORGE_DATA_HOME" "$AIFORGE_CONFIG_DIR"
exec "$HERE/app/first-run.sh" --open "$@"
LAUNCH
  return
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
if exist "%HERE%app\wheels" set "AIFORGE_WHEEL_DIR=%HERE%app\wheels"
if not exist "%AIFORGE_DATA_HOME%" mkdir "%AIFORGE_DATA_HOME%"
if not exist "%AIFORGE_CONFIG_DIR%" mkdir "%AIFORGE_CONFIG_DIR%"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%HERE%app\first-run.ps1" --open %*
endlocal
CMD
    ;;
  *)
    echo "build-portable: unknown target: $TARGET" >&2; exit 2 ;;
esac

cat > "$ROOT/README.txt" <<TXT
AIForge $VERSION — portable

  Run it:   $( [[ "$TARGET" == windows ]] && echo "AIForge.cmd" \
              || { [[ "$TARGET" == macos* ]] && echo "AIForge.command" || echo "./AIForge.sh"; } )
  Then:     http://localhost:8799/ui/

Nothing is installed and nothing is written outside this folder. Your memory,
tickets, chat history and the Python runtime all live in data/ — copy the whole
folder to another machine and it carries its history with it.

Needs Python 3.12 on this machine (macOS: the python.org installer;
Ubuntu 24.04: sudo apt install python3.12; Windows: winget install Python.Python.3.12).

$( [[ -d "$ROOT/app/wheels" ]] \
   && echo "This is the OFFLINE bundle: every dependency is included, so the first run needs no network." \
   || echo "The FIRST run installs the dependencies into data/runtime — it needs the package index once. Every run after that does not." )

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
