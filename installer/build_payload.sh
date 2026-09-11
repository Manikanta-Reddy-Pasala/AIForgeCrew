#!/usr/bin/env bash
# Build the thing all three packages carry, once.
#
# The payload is deliberately small and identical everywhere:
#
#   aiforge-<version>-py3-none-any.whl   the app, UI already built into it
#   uv-<target>                          a single static binary (from uv's PyPI wheel)
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

# uv ships as a PyPI wheel per platform, with the static binary inside
# (uv-<ver>.data/scripts/). Taking it from the index — not GitHub releases — is
# the same source, mirror, CA and pin as every other dependency.
uv_platform() {
  local target="$1"
  case "$target" in
    macos)       echo "macosx_11_0_arm64" ;;
    macos-x64)   echo "macosx_10_12_x86_64" ;;
    linux)       echo "manylinux_2_17_x86_64" ;;
    linux-arm64) echo "manylinux_2_17_aarch64" ;;
    windows)     echo "win_amd64" ;;
    *) return 1 ;;
  esac
}

# The internal Artifactory is the only source — same rule as run.sh, no public
# fallback. PyPI index: UV_DEFAULT_INDEX, else pyproject's [[tool.uv.index]].
# npm: npm_config_registry, else AIFORGE_NPM_REGISTRY from aiforge.env.
# The internal CA is in the OS trust store; uv and Node ignore it unless told.
export UV_SYSTEM_CERTS="${UV_SYSTEM_CERTS:-true}" NODE_USE_SYSTEM_CA="${NODE_USE_SYSTEM_CA:-1}"

pick_index() {
  INDEX="${UV_DEFAULT_INDEX:-${UV_INDEX_URL:-$(sed -n '/^\[\[tool\.uv\.index\]\]/,/^\[/s/^url *= *"\(.*\)"/\1/p' \
          "$REPO_ROOT/pyproject.toml" | head -1 || true)}}"
  [[ -n "$INDEX" ]] || { echo "no package index (pyproject / UV_DEFAULT_INDEX)" >&2; exit 1; }
  local host="${INDEX#*://}"; host="${host%%[:/]*}"
  if ! getent hosts "$host" >/dev/null 2>&1 \
     && ! python3 -c 'import socket, sys; socket.getaddrinfo(sys.argv[1], 443)' "$host" >/dev/null 2>&1; then
    echo "package index host '$host' does not resolve — builds use only $INDEX" >&2; exit 1
  fi
  export UV_DEFAULT_INDEX="$INDEX"
  NPM_REGISTRY="${npm_config_registry:-$(sed -n 's/^AIFORGE_NPM_REGISTRY=//p' "$REPO_ROOT/aiforge.env" 2>/dev/null | head -1)}"
  [[ -n "$NPM_REGISTRY" ]] || { echo "no npm registry (aiforge.env AIFORGE_NPM_REGISTRY)" >&2; exit 1; }
  echo "==> index: $INDEX   npm: $NPM_REGISTRY"
  return 0
}

# The uv every package ships is the one uv.lock pins — the same uv run.sh and
# CI use — not whatever GitHub calls latest on the day of the build.
uv_version() {
  sed -n '/^name = "uv"$/{n;s/^version = "\(.*\)"/\1/p;}' "$REPO_ROOT/uv.lock" | head -1
  return
}

version() {
  # The single source of truth is pyproject; a version baked anywhere else
  # eventually disagrees with the wheel the package installs.
  grep -m1 '^version' "$REPO_ROOT/pyproject.toml" | cut -d'"' -f2
  return
}

build_wheel() {
  echo "==> building the web UI (it ships INSIDE the wheel — no npm on the target)"
  if [[ -f "$REPO_ROOT/web/package.json" ]]; then
    ( cd "$REPO_ROOT/web" && npm_config_update_notifier=false npm_config_registry="$NPM_REGISTRY" \
        npm ci --ignore-scripts --no-audit --no-fund --loglevel=error && npm run build --silent )
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
  # `uv build`, not `uv run --with build`: uv run first SYNCS the project env
  # (every extra, against the index) just to run a build frontend.
  echo "==> building the wheel"
  ( cd "$REPO_ROOT" && uv build --wheel --out-dir "$OUT" )
  # aiforge-memory is vendored in-repo and resolved through [tool.uv.sources],
  # which is a LOCK-time mechanism: it does not travel in the wheel's metadata,
  # so an installed package looks for it on PyPI and does not find it. Ship its
  # wheel next to the app's and let --find-links resolve it offline.
  if [[ -d "$REPO_ROOT/packages/aiforge_memory" ]]; then
    echo "==> building the vendored aiforge-memory wheel"
    ( cd "$REPO_ROOT/packages/aiforge_memory" && uv build --wheel --out-dir "$OUT" )
  fi
  # Every version uv.lock pins, which each first run installs as --override.
  # Without it a package resolves fresh from the index and none of pyproject's
  # override-dependencies apply — they are a uv PROJECT setting and never reach
  # wheel metadata: a packaged install got starlette 0.52.1, below the >=1.1.0
  # floor that exists because <1.0 carries two HIGH CVEs. OVERRIDE, not -c:
  # google-adk 2.1.0 itself caps starlette <1.0, so as constraints the lock is
  # unsatisfiable — the lock only exists because pyproject overrides that cap.
  echo "==> exporting lock-pins.txt from uv.lock"
  ( cd "$REPO_ROOT" && uv export --frozen --all-extras --no-dev --no-hashes \
      --no-emit-project --no-emit-local --quiet -o "$OUT/lock-pins.txt" )
  # The index every first run installs from: an installed app has no
  # pyproject to read it from, and uv's own default would be pypi.org.
  printf '%s\n' "$INDEX" > "$OUT/index-url.txt"
  return
}

fetch_uv() {
  local target="$1" plat dest ver tmp
  plat="$(uv_platform "$target")" || { echo "unknown target: $target" >&2; return 1; }
  dest="$OUT/uv/$target"
  ver="$(uv_version)"
  [[ -n "$ver" ]] || { echo "no uv version in uv.lock" >&2; return 1; }
  # Already there at the locked version → keep it. A different one is replaced.
  if [[ "$(cat "$dest/.version" 2>/dev/null)" == "$ver" ]] && compgen -G "$dest/uv*" >/dev/null; then
    echo "==> uv $ver for $target already present"
    return 0
  fi
  rm -rf "$dest" && mkdir -p "$dest"
  tmp="$(mktemp -d)"
  echo "==> uv $ver for $target (PyPI wheel, $plat)"
  "$PYTHON" -m pip download --quiet --disable-pip-version-check --no-deps \
      --only-binary=:all: --platform "$plat" ${PIP_INDEX_ARGS[@]+"${PIP_INDEX_ARGS[@]}"} \
      "uv==$ver" -d "$tmp"
  "$PYTHON" - "$tmp" "$dest" <<'PY'
import glob, os, sys, zipfile
tmp, dest = sys.argv[1:]
whl, = glob.glob(os.path.join(tmp, "uv-*.whl"))
with zipfile.ZipFile(whl) as z:
    for info in z.infolist():
        head, _, name = info.filename.rpartition("/")
        if head.endswith(".data/scripts") and name:
            out = os.path.join(dest, name)
            with open(out, "wb") as f:
                f.write(z.read(info))
            os.chmod(out, 0o755)
PY
  rm -rf "$tmp"
  compgen -G "$dest/uv*" >/dev/null || { echo "no uv binary in the $plat wheel" >&2; return 1; }
  printf '%s' "$ver" > "$dest/.version"
}

mkdir -p "$OUT"
echo "==> AIForge $(version) → $OUT"
pick_index
PYTHON="${PYTHON:-python3}"
PIP_INDEX_ARGS=(--index-url "$INDEX")
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
