#!/usr/bin/env bash
# Build aiforge_<version>_amd64.deb — Ubuntu/Debian.
#
# Layout, and why:
#   /opt/aiforge/            the payload (wheel + uv). Root-owned, read-only,
#                            replaced wholesale by an upgrade.
#   /usr/bin/aiforge         a wrapper that builds the CURRENT USER's venv on
#                            first run and then execs it.
#   ~/.local/share/aiforge/  that venv. Per-user on purpose: the agent runs as
#                            you and writes your repos, so the runtime it can
#                            repair must be yours too.
#
# Depends: python3 is NOT a dependency. uv provisions 3.12 itself, which is the
# only way one .deb works on 22.04 (python3.10) and 24.04 (3.12) alike. tmux IS
# a dependency: without it the agent's bash tool silently loses `cd`/`export`
# between calls, and a package is exactly where that should be settled.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PAYLOAD="$REPO_ROOT/dist/installer"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

VERSION="$(grep -m1 '^version' "$REPO_ROOT/pyproject.toml" | cut -d'"' -f2)"
# Build for the arch we are ON unless told otherwise: a .deb whose control
# says amd64 simply refuses to install on arm64, and "it built fine" is not the
# same as "it installs".
ARCH="${DEB_ARCH:-$(dpkg --print-architecture 2>/dev/null || echo amd64)}"
case "$ARCH" in
  amd64) UV_DIR=linux ;;
  arm64) UV_DIR=linux-arm64 ;;
  *) echo "no uv build packaged for dpkg arch '$ARCH'" >&2; exit 1 ;;
esac
PKG="$PAYLOAD/aiforge_${VERSION}_${ARCH}.deb"

WHEEL="$(ls -1 "$PAYLOAD"/aiforgecrew-*.whl 2>/dev/null | head -1 || true)"
[[ -n "$WHEEL" ]] || { echo "no wheel — run installer/build_payload.sh first" >&2; exit 1; }
[[ -x "$PAYLOAD/uv/$UV_DIR/uv" ]] || {
  echo "no uv for $ARCH — run installer/build_payload.sh --target $UV_DIR" >&2; exit 1; }

install -d "$STAGE/DEBIAN" "$STAGE/opt/aiforge/uv" "$STAGE/usr/bin" \
           "$STAGE/usr/share/applications" "$STAGE/usr/share/doc/aiforge"

# Every wheel in the payload: the app's, plus the vendored aiforge-memory that
# no index carries.
for w in "$PAYLOAD"/*.whl; do install -m 0644 "$w" "$STAGE/opt/aiforge/"; done
install -m 0755 "$PAYLOAD/uv/$UV_DIR/uv"    "$STAGE/opt/aiforge/uv/uv"
install -m 0755 "$REPO_ROOT/installer/common/first-run.sh" "$STAGE/opt/aiforge/first-run.sh"

cat > "$STAGE/usr/bin/aiforge" <<'WRAP'
#!/usr/bin/env bash
# Thin wrapper: the package is immutable, the runtime is yours.
export AIFORGE_APP_HOME=/opt/aiforge
export AIFORGE_DATA_HOME="${AIFORGE_DATA_HOME:-$HOME/.local/share/aiforge}"
exec /opt/aiforge/first-run.sh "$@"
WRAP
chmod 0755 "$STAGE/usr/bin/aiforge"

cat > "$STAGE/usr/share/applications/aiforge.desktop" <<DESK
[Desktop Entry]
Type=Application
Name=AIForge
Comment=Local AI engineering crew — API, chat and the ticket pipeline
Exec=/usr/bin/aiforge --open
Terminal=true
Categories=Development;
DESK
chmod 0644 "$STAGE/usr/share/applications/aiforge.desktop"

install -m 0644 "$REPO_ROOT/installer/README.md" "$STAGE/usr/share/doc/aiforge/README.md"

INSTALLED_KB="$(du -ks "$STAGE" | cut -f1)"
cat > "$STAGE/DEBIAN/control" <<CTRL
Package: aiforge
Version: $VERSION
Section: devel
Priority: optional
Architecture: $ARCH
Depends: ca-certificates, curl, git, tmux
Installed-Size: $INSTALLED_KB
Maintainer: AIForge <noreply@aiforge.local>
Description: Local AI engineering crew
 AIForge runs an API, a chat UI and an autonomous ticket pipeline against your
 own repositories, with its memory and state in your home directory.
 .
 The interpreter is NOT a dependency: the package carries uv, which provisions
 the Python it needs on first launch. That first launch needs the network; every
 launch after it does not.
CTRL

cat > "$STAGE/DEBIAN/postinst" <<'POST'
#!/bin/sh
set -e
# Deliberately does NOT build a venv here. postinst runs as root, and a
# root-built runtime is one the user who actually runs the agent cannot repair.
# The first `aiforge` does it, as them, in their home.
echo "AIForge installed. Run 'aiforge' to start it (first run downloads its Python)."
exit 0
POST
chmod 0755 "$STAGE/DEBIAN/postinst"

cat > "$STAGE/DEBIAN/postrm" <<'PRM'
#!/bin/sh
set -e
# Leaves ~/.local/share/aiforge and ~/.aiforge alone on remove: that is the
# user's memory, tickets and chat history, not package state. `purge` says so.
if [ "$1" = "purge" ]; then
  echo "AIForge removed. Per-user data is still in ~/.local/share/aiforge and ~/.aiforge."
fi
exit 0
PRM
chmod 0755 "$STAGE/DEBIAN/postrm"

dpkg-deb --build --root-owner-group "$STAGE" "$PKG" >/dev/null
echo "==> $PKG"
dpkg-deb --info "$PKG" | sed 's/^/    /'
