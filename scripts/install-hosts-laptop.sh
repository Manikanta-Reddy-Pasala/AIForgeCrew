#!/usr/bin/env bash
# scripts/install-hosts-laptop.sh — add paperclip.local + hermes.local to laptop /etc/hosts
# so the laptop browser resolves them to the Mac Studio (which runs Caddy on :80).
#
# Needs sudo.
set -euo pipefail

MAC_STUDIO_IP="${MAC_STUDIO_IP:-192.168.70.185}"
LINE="$MAC_STUDIO_IP paperclip.lan hermes.lan"

# NOTE: macOS routes *.local via mDNSResponder and ignores /etc/hosts for
# .local names (getaddrinfo times out). Use .lan instead.

if grep -qxF "$LINE" /etc/hosts; then
  echo "[skip] /etc/hosts already has: $LINE"
  exit 0
fi

# Remove any stale AIForgeCrew-tagged lines (.local or old .lan).
if grep -qE "paperclip\.(local|lan)|hermes\.(local|lan)" /etc/hosts; then
  echo "Cleaning old paperclip/hermes entries..."
  sudo sed -i.bak -E "/paperclip\.(local|lan)|hermes\.(local|lan)/d" /etc/hosts
fi

echo ">>> adding to /etc/hosts (sudo)"
echo "$LINE" | sudo tee -a /etc/hosts >/dev/null

echo
echo "Now open:"
echo "  http://paperclip.lan    (Paperclip UI)"
echo "  http://hermes.lan       (Hermes dashboard)"
echo
echo "Backup of original at /etc/hosts.bak"
