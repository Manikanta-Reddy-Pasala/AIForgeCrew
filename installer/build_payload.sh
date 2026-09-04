#!/usr/bin/env bash
# Build the thing all three packages carry, once.
#
# The payload is deliberately small and identical everywhere:
#
#   aiforge-<version>-py3-none-any.whl   the app, UI already built into it
#   uv-<target>                          a single static binary
#
# and NOT a Python. Every OS ships a different, usually too-old Python (Ubuntu
# 22.04 is on 3.10, macOS system python is 3.9, Windows may have none), and
# bundling a 60MB interpreter per target to dodge that is how installers get to
# 500MB. uv provisions the 3.12 it needs on first run instead — one binary,
# every platform, and the same code path that already works in run.sh.
#
#   installer/build_payload.sh [--target macos|linux|windows] [--out DIR]
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$REPO_ROOT/dist/installer"
TARGET=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target) TARGET="$2"; shift 2 ;;
    --out)    OUT="$2";    shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

# uv publishes one static binary per platform; these are the three we package.
uv_asset() {
  case "$1" in
    macos)       echo "uv-aarch64-apple-darwin.tar.gz" ;;
    macos-x64)   echo "uv-x86_64-apple-darwin.tar.gz" ;;
    linux)       echo "uv-x86_64-unknown-linux-gnu.tar.gz" ;;
    linux-arm64) echo "uv-aarch64-unknown-linux-gnu.tar.gz" ;;
    windows)     echo "uv-x86_64-pc-windows-msvc.zip" ;;
    *) return 1 ;;
  esac
}

version() {
  # The single source of truth is pyproject; a version baked anywhere else
  # eventually disagrees with the wheel the package installs.
  grep -m1 '^version' "$REPO_ROOT/pyproject.toml" | cut -d'"' -f2
}

build_wheel() {
  echo "==> building the web UI (it ships INSIDE the wheel — no npm on the target)"
  if [[ -f "$REPO_ROOT/web/package.json" ]]; then
    ( cd "$REPO_ROOT/web" && npm ci --ignore-scripts --no-audit --no-fund && npm run build )
  fi
  # The API resolves its UI from aiforge_core/web_dist when installed (see the
  # two candidates in api.py) — the repo path does not exist inside site-packages.
  echo "==> staging the UI into the package (aiforge_core/web_dist)"
  rm -rf "$REPO_ROOT/aiforge_core/web_dist"
  if [[ -d "$REPO_ROOT/web/dist" ]]; then
    cp -R "$REPO_ROOT/web/dist" "$REPO_ROOT/aiforge_core/web_dist"
  else
    echo "    ! no web/dist — the package will serve an API with no UI" >&2
  fi
  echo "==> building the wheel"
  ( cd "$REPO_ROOT" && uv run --with build python -m build --wheel --outdir "$OUT" )
  # aiforge-memory is vendored in-repo and resolved through [tool.uv.sources],
  # which is a LOCK-time mechanism: it does not travel in the wheel's metadata,
  # so an installed package looks for it on PyPI and does not find it. Ship its
  # wheel next to the app's and let --find-links resolve it offline.
  if [[ -d "$REPO_ROOT/packages/aiforge_memory" ]]; then
    echo "==> building the vendored aiforge-memory wheel"
    ( cd "$REPO_ROOT/packages/aiforge_memory" \
      && uv run --with build python -m build --wheel --outdir "$OUT" )
  fi
}

fetch_uv() {
  local target="$1" asset dest url
  asset="$(uv_asset "$target")" || { echo "unknown target: $target" >&2; return 1; }
  dest="$OUT/uv/$target"
  mkdir -p "$dest"
  # Already there → keep it. These are release assets, not a moving target, and
  # a rebuild should not re-download 30MB per package.
  if compgen -G "$dest/uv*" >/dev/null; then
    echo "==> uv for $target already present"
    return 0
  fi
  url="https://github.com/astral-sh/uv/releases/latest/download/$asset"
  echo "==> fetching uv for $target"
  # --proto '=https': the release URL is https and a redirect may
  # not move it to cleartext.
  curl --proto '=https' --tlsv1.2 -fsSL "$url" -o "$dest/$asset"
  ( cd "$dest" && case "$asset" in
      *.tar.gz) tar -xzf "$asset" --strip-components=1 ;;
      *.zip)    unzip -qo "$asset" ;;
    esac
    rm -f "$asset" )
  chmod +x "$dest"/uv* 2>/dev/null || true
}

mkdir -p "$OUT"
echo "==> AIForge $(version) → $OUT"
build_wheel
if [[ -n "$TARGET" ]]; then
  fetch_uv "$TARGET"
else
  # Both Linux arches: one .deb per architecture, and the ARM one is not
  # exotic any more (Graviton, Ampere, an Apple-silicon container).
  for t in macos linux linux-arm64 windows; do fetch_uv "$t"; done
fi
echo "==> payload ready:"
ls -la "$OUT" | sed 's/^/    /'
