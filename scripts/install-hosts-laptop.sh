#!/usr/bin/env bash
# scripts/install-hosts-laptop.sh — add paperclip.local + hermes.local to laptop /etc/hosts
# so the laptop browser resolves them to the Mac Studio (which runs Caddy on :80).
#
# Needs sudo.
set -euo pipefail

MAC_STUDIO_IP="${MAC_STUDIO_IP:-192.168.70.185}"
LINE="$MAC_STUDIO_IP paperclip.local hermes.local"

if grep -qxF "$LINE" /etc/hosts; then
  echo "[skip] /etc/hosts already has: $LINE"
  exit 0
fi

# Remove any stale AIForgeCrew-tagged line first, then append fresh.
if grep -q "paperclip.local" /etc/hosts; then
  echo "Cleaning old paperclip.local entries..."
  sudo sed -i.bak '/paperclip\.local/d' /etc/hosts
fi

echo ">>> adding to /etc/hosts (sudo)"
echo "$LINE" | sudo tee -a /etc/hosts >/dev/null

echo
echo "Now open:"
echo "  http://paperclip.local    (Paperclip UI)"
echo "  http://hermes.local       (Hermes dashboard)"
echo
echo "Backup of original at /etc/hosts.bak"
