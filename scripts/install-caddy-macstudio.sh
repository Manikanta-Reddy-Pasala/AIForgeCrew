#!/usr/bin/env bash
# scripts/install-caddy-macstudio.sh — install Caddy on Mac Studio as a
# reverse proxy so paperclip.local / hermes.local resolve on :80.
#
# Steps:
#   1. Download the static Caddy darwin/arm64 binary to ~/.local/bin/caddy.
#   2. Write a Caddyfile at ~/.config/caddy/Caddyfile.
#   3. Add 127.0.0.1 paperclip.local hermes.local to /etc/hosts (needs sudo).
#   4. Install LaunchDaemon so Caddy binds :80 as root across reboots (sudo).
#
# Run on Mac Studio (ssh works — steps 3 + 4 will prompt for sudo password).
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "install-caddy: macOS only" >&2; exit 1
fi

CADDY_BIN="$HOME/.local/bin/caddy"
CADDY_VER="${CADDY_VER:-2.8.4}"
TAR="/tmp/caddy_${CADDY_VER}_darwin_arm64.tar.gz"
URL="https://github.com/caddyserver/caddy/releases/download/v${CADDY_VER}/caddy_${CADDY_VER}_mac_arm64.tar.gz"

# --- 1. binary ---
if [[ ! -x "$CADDY_BIN" ]] || ! "$CADDY_BIN" version 2>/dev/null | grep -q "v$CADDY_VER"; then
  echo ">>> downloading caddy v$CADDY_VER"
  mkdir -p "$HOME/.local/bin"
  curl -fsSL "$URL" -o "$TAR"
  tar -xzf "$TAR" -C /tmp caddy
  mv /tmp/caddy "$CADDY_BIN"
  chmod +x "$CADDY_BIN"
fi
"$CADDY_BIN" version

# --- 2. Caddyfile ---
CADDY_CONF="$HOME/.config/caddy"
mkdir -p "$CADDY_CONF"
cat > "$CADDY_CONF/Caddyfile" <<'EOF'
# AIForgeCrew reverse proxy.
# Both vhosts plain HTTP on :80 (no cert — .local names can't get public LE).
{
    auto_https off
    admin off
}

http://paperclip.local, http://paperclip {
    reverse_proxy 127.0.0.1:3100
}

http://hermes.local, http://hermes {
    reverse_proxy 127.0.0.1:9119
}

# Fallback on any other Host header: show a tiny index so misdirected requests
# don't 404 confusingly.
:80 {
    respond "AIForgeCrew — try http://paperclip.local or http://hermes.local" 200
}
EOF
echo "wrote $CADDY_CONF/Caddyfile"

# --- 3. /etc/hosts ---
HOSTS_LINE="127.0.0.1 paperclip.local hermes.local"
if ! grep -qxF "$HOSTS_LINE" /etc/hosts; then
  echo ">>> adding hosts entry (needs sudo)"
  echo "$HOSTS_LINE" | sudo tee -a /etc/hosts >/dev/null
fi

# --- 4. LaunchDaemon (root) so Caddy can bind :80 ---
DAEMON=/Library/LaunchDaemons/com.aiforge.caddy.plist
LOG_DIR="$HOME/aiforge-logs"
mkdir -p "$LOG_DIR"

sudo tee "$DAEMON" >/dev/null <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.aiforge.caddy</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ProgramArguments</key>
    <array>
        <string>$CADDY_BIN</string>
        <string>run</string>
        <string>--config</string>
        <string>$CADDY_CONF/Caddyfile</string>
        <string>--adapter</string>
        <string>caddyfile</string>
    </array>
    <key>StandardOutPath</key>
    <string>$LOG_DIR/caddy.out.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/caddy.err.log</string>
    <key>WorkingDirectory</key>
    <string>$CADDY_CONF</string>
</dict>
</plist>
PLIST
sudo chown root:wheel "$DAEMON"
sudo chmod 644 "$DAEMON"

sudo launchctl bootout system/com.aiforge.caddy 2>/dev/null || true
sudo launchctl bootstrap system "$DAEMON"
sudo launchctl enable system/com.aiforge.caddy
echo "caddy LaunchDaemon loaded."

sleep 3
echo
echo "=== verify ==="
curl -s -o /dev/null -w "  http://paperclip.local → %{http_code}\n" http://paperclip.local/
curl -s -o /dev/null -w "  http://hermes.local    → %{http_code}\n" http://hermes.local/
