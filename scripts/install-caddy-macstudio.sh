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
# Both vhosts plain HTTP on :80 (no cert — internal .lan names).
#
# NOTE: `.local` TLD is intercepted by macOS mDNSResponder and bypasses
# /etc/hosts, causing curl/browser resolve timeouts. Use `.lan` (or any
# non-.local TLD) so getaddrinfo actually reads /etc/hosts.
{
    auto_https off
    admin off
}

http://paperclip.lan, http://paperclip {
    reverse_proxy 127.0.0.1:3100
}

http://hermes.lan, http://hermes {
    reverse_proxy 127.0.0.1:9119
}

# Fallback on any other Host header: show a tiny index so misdirected requests
# don't 404 confusingly.
:80 {
    respond "AIForgeCrew — try http://paperclip.lan or http://hermes.lan" 200
}
EOF
echo "wrote $CADDY_CONF/Caddyfile"

# --- 3. /etc/hosts ---
# On the Mac Studio itself, 127.0.0.1 is fine — Caddy listens locally.
HOSTS_LINE="127.0.0.1 paperclip.lan hermes.lan"
# Strip any stale .local entries that would shadow the new names.
if grep -qE "paperclip\.(local|lan)|hermes\.(local|lan)" /etc/hosts; then
  sudo sed -i.bak -E "/paperclip\.(local|lan)|hermes\.(local|lan)/d" /etc/hosts
fi
echo "$HOSTS_LINE" | sudo tee -a /etc/hosts >/dev/null
echo "wrote /etc/hosts: $HOSTS_LINE"

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

# --- 5. allow paperclip.lan in Paperclip (else it returns 403) ---
export PATH="$HOME/.hermes/node/bin:$PATH"
if command -v npx >/dev/null; then
  echo ">>> allowing paperclip.lan in Paperclip config"
  npx -y paperclipai allowed-hostname paperclip.lan 2>&1 | tail -3 || true
  UID_=$(id -u)
  launchctl kickstart -k "gui/$UID_/com.aiforge.paperclip" 2>/dev/null || true
  # Wait for Paperclip to come back.
  for _ in $(seq 1 30); do
    curl -sf http://localhost:3100/api/health >/dev/null 2>&1 && break
    sleep 2
  done
fi

echo
echo "=== verify ==="
curl -s -o /dev/null -w "  http://paperclip.lan → %{http_code}\n" http://paperclip.lan/
curl -s -o /dev/null -w "  http://hermes.lan    → %{http_code}\n" http://hermes.lan/
