#!/usr/bin/env bash
# install-wireguard: bring up the WG client on NUC.
#
# We previously ran the WG client on Mac Studio. Moved to NUC on 2026-04-24
# so the aiforge-api (:8799) is reachable over the WG network (10.66.66.3)
# and therefore via the reverse proxy at 77.42.45.12:9443 from anywhere.
#
# Usage:
#   scp scripts/runtime/nuc/install-wireguard.sh mani@nuc:/tmp/
#   scp <source-of-wg0.conf> mani@nuc:/tmp/wg0.conf
#   ssh mani@nuc 'sudo bash /tmp/install-wireguard.sh /tmp/wg0.conf'
#
# The script is idempotent. Running it a second time is a no-op.

set -euo pipefail

CONF_SRC="${1:-}"
if [[ -z "$CONF_SRC" || ! -f "$CONF_SRC" ]]; then
  echo "usage: $0 <path/to/wg0.conf>" >&2
  exit 2
fi

if [[ $EUID -ne 0 ]]; then
  echo "must run as root (use sudo)" >&2
  exit 2
fi

# 1. Install wireguard.
if ! command -v wg >/dev/null; then
  apt-get update -qq
  apt-get install -y wireguard wireguard-tools
fi

# 2. Install config with root-only perms.
install -m 600 -o root -g root "$CONF_SRC" /etc/wireguard/wg0.conf

# 3. Enable + start systemd unit.
systemctl enable wg-quick@wg0 >/dev/null
systemctl restart wg-quick@wg0

sleep 2
wg show wg0 | head -10
echo "WG client up. Route via 10.66.66.0/24 ready."
