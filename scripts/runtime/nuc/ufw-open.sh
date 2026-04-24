#!/usr/bin/env bash
# ufw-open: declarative NUC firewall for AIForge.
#
# Captures every UFW rule aiforge services need. Re-runnable; UFW dedupes
# identical rules so this is safe as a boot-time or ansible-style ensure.
#
# Run:
#   scp scripts/runtime/nuc/ufw-open.sh mani@nuc:/tmp/
#   ssh mani@nuc 'sudo bash /tmp/ufw-open.sh'
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "must run as root (use sudo)" >&2
  exit 2
fi

# Keep-alive defaults. Existing SSH (22) should already be allowed so we do
# NOT enable UFW here if it's disabled — that would lock us out on first run.
if ! ufw status | grep -q "Status: active"; then
  echo "UFW is inactive. Enable manually after confirming SSH rule:" >&2
  echo "  sudo ufw allow 22/tcp && sudo ufw enable" >&2
  exit 1
fi

# LAN (192.168.70.0/24) — laptop + other devices on the home network.
ufw allow from 192.168.70.0/24 to any port 5432 comment 'postgres (LAN)' || true
ufw allow from 192.168.70.0/24 to any port 7474 comment 'neo4j http (LAN)' || true
ufw allow from 192.168.70.0/24 to any port 7687 comment 'neo4j bolt (LAN)' || true
ufw allow from 192.168.70.0/24 to any port 8799 comment 'aiforge api (LAN)' || true
ufw allow from 192.168.70.0/24 to any port 8810:8813 proto tcp comment 'aiforge misc (LAN)' || true
ufw allow from 192.168.70.0/24 to any port 8820:8823 proto tcp comment 'aiforge misc (LAN)' || true

# WireGuard peer network (10.66.66.0/24) — required for the reverse proxy
# at 77.42.45.12:9443 to reach aiforge-api via WG. Without this the nginx
# upstream gets HTTP 504.
ufw allow from 10.66.66.0/24 to any port 8799 comment 'aiforge api (WG)' || true

# Ollama (internet-exposed model API — intentional).
ufw allow 11434/tcp comment 'ollama (public)' || true

echo "ufw status:"
ufw status | grep -E "^(8799|7474|7687|5432|10\.66|11434)"
