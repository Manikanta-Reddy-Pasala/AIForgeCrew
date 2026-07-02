#!/usr/bin/env bash
# Pin the Mac Studio to a static IP so it never drifts on DHCP.
# Run ON the Mac Studio with sudo. Uses `networksetup` (no reboot needed).
#
#   sudo scripts/net/set-static-ip-mac-studio.sh
#
# Override via env:
#   MS_IP=192.168.70.116  MS_MASK=255.255.255.0  MS_GW=192.168.70.1
#   MS_DNS="192.168.70.1 1.1.1.1"  MS_SERVICE=<auto: first active service>
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }

IP="${MS_IP:-192.168.70.116}"
MASK="${MS_MASK:-255.255.255.0}"
GW="${MS_GW:-192.168.70.1}"
DNS="${MS_DNS:-192.168.70.1 1.1.1.1}"

# Auto-detect the active network service (Ethernet preferred, else Wi-Fi).
if [[ -z "${MS_SERVICE:-}" ]]; then
  MS_SERVICE=$(networksetup -listallnetworkservices | tail -n +2 | while read -r svc; do
    if networksetup -getinfo "$svc" 2>/dev/null | grep -q "IP address: [0-9]"; then
      echo "$svc"; break
    fi
  done)
fi
[[ -n "$MS_SERVICE" ]] || { echo "could not detect an active network service — set MS_SERVICE" >&2; exit 1; }

echo "==> pinning '$MS_SERVICE' -> $IP mask $MASK gw $GW dns $DNS"
networksetup -setmanual "$MS_SERVICE" "$IP" "$MASK" "$GW"
networksetup -setdnsservers "$MS_SERVICE" $DNS
sleep 2
echo "==> now:"; networksetup -getinfo "$MS_SERVICE" | grep -E "IP address|Subnet|Router"
echo "==> done. The Mac Studio keeps $IP. Update AIFORGE_MS_HOST=$IP in your"
echo "    deploy env. (Revert with: networksetup -setdhcp \"$MS_SERVICE\")"
