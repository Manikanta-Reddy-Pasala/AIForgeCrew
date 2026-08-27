#!/usr/bin/env bash
# Build AIForge-<version>.dmg — a drag-to-Applications disk image containing
# AIForge.app.
#
# The .app is a launcher, not a frozen interpreter: Contents/MacOS/AIForge sets
# APP_HOME/DATA_HOME and hands off to the same first-run.sh the .deb uses, so
# macOS and Ubuntu cannot drift. Stock macOS python is 3.9, so uv provisions
# 3.12 on first launch exactly as it does everywhere else.
#
# NOT signed and NOT notarised — that needs an Apple Developer ID this build
# does not have. Gatekeeper will refuse the first open; the README says how
# (right-click → Open, or xattr -d com.apple.quarantine). Sign it in CI with
# `codesign --deep --sign "Developer ID Application: …"` before shipping it to
# anyone who did not build it.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PAYLOAD="$REPO_ROOT/dist/installer"
VERSION="$(grep -m1 '^version' "$REPO_ROOT/pyproject.toml" | cut -d'"' -f2)"
UV_TARGET="${UV_TARGET:-macos}"          # macos = arm64; macos-x64 for Intel
DMG="$PAYLOAD/AIForge-${VERSION}.dmg"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

WHEEL="$(ls -1 "$PAYLOAD"/aiforgecrew-*.whl 2>/dev/null | head -1 || true)"
[[ -n "$WHEEL" ]] || { echo "no wheel — run installer/build_payload.sh first" >&2; exit 1; }
[[ -x "$PAYLOAD/uv/$UV_TARGET/uv" ]] || {
  echo "no macOS uv — run installer/build_payload.sh --target $UV_TARGET" >&2; exit 1; }

APP="$STAGE/AIForge.app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/uv"

# Every wheel: the app's, plus the vendored aiforge-memory no index carries.
cp "$PAYLOAD"/*.whl "$APP/Contents/Resources/"
cp "$PAYLOAD/uv/$UV_TARGET/uv" "$APP/Contents/Resources/uv/uv"
cp "$REPO_ROOT/installer/common/first-run.sh" "$APP/Contents/Resources/first-run.sh"
chmod 0755 "$APP/Contents/Resources/uv/uv" "$APP/Contents/Resources/first-run.sh"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>AIForge</string>
  <key>CFBundleDisplayName</key><string>AIForge</string>
  <key>CFBundleIdentifier</key><string>local.aiforge.app</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>AIForge</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <!-- The launcher opens Terminal so the log is visible: this app IS a server
       plus two background loops, and a silent dock icon with no window is how
       people end up with three copies running. -->
  <key>LSBackgroundOnly</key><false/>
</dict>
</plist>
PLIST

cat > "$APP/Contents/MacOS/AIForge" <<'LAUNCH'
#!/usr/bin/env bash
# Double-clicked from Finder there is no terminal attached, so re-launch into
# one: this app is a server and two loops, and its first run prints what it is
# downloading. A silent bounce in the Dock is indistinguishable from a failure.
set -euo pipefail
RES="$(cd "$(dirname "${BASH_SOURCE[0]}")/../Resources" && pwd)"
export AIFORGE_APP_HOME="$RES"
export AIFORGE_DATA_HOME="${AIFORGE_DATA_HOME:-$HOME/Library/Application Support/AIForge}"

if [[ -t 1 ]]; then
  exec "$RES/first-run.sh" --open
fi
# Quote for AppleScript, then hand the same command to Terminal.
CMD="AIFORGE_APP_HOME='$RES' AIFORGE_DATA_HOME='$AIFORGE_DATA_HOME' '$RES/first-run.sh' --open"
osascript -e "tell application \"Terminal\" to do script \"$CMD\"" \
          -e 'tell application "Terminal" to activate' >/dev/null
LAUNCH
chmod 0755 "$APP/Contents/MacOS/AIForge"

ln -s /Applications "$STAGE/Applications"
cp "$REPO_ROOT/installer/README.md" "$STAGE/README.md"

rm -f "$DMG"
hdiutil create -volname "AIForge $VERSION" -srcfolder "$STAGE" -ov -quiet \
               -format UDZO "$DMG"
echo "==> $DMG"
hdiutil imageinfo "$DMG" | grep -E "^(Format|Checksum Type):" | sed 's/^/    /'
