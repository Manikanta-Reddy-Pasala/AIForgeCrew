#!/usr/bin/env bash
# Pin the NUC (Ubuntu / netplan) to a static IP so it never drifts on DHCP.
# Run ON the NUC with sudo. Idempotent — writes a dedicated netplan file.
#
#   sudo scripts/net/set-static-ip-nuc.sh
#
# Override via env:
#   NUC_IP=192.168.70.115  NUC_CIDR=24  NUC_GW=192.168.70.1
#   NUC_DNS="192.168.70.1,1.1.1.1"  NUC_IFACE=<auto-detected primary>
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }

IP="${NUC_IP:-192.168.70.115}"
CIDR="${NUC_CIDR:-24}"
GW="${NUC_GW:-192.168.70.1}"
DNS="${NUC_DNS:-192.168.70.1,1.1.1.1}"
# Auto-detect the primary wired interface if not given.
IFACE="${NUC_IFACE:-$(ip -o -4 route show to default | awk '{print $5}' | head -1)}"
[[ -n "$IFACE" ]] || { echo "could not detect interface — set NUC_IFACE" >&2; exit 1; }

FILE="/etc/netplan/99-aiforge-static.yaml"
echo "==> pinning $IFACE -> $IP/$CIDR (gw $GW, dns $DNS) via $FILE"
DNS_YAML="[$(echo "$DNS" | sed 's/,/, /g')]"
cat > "$FILE" <<YAML
# Managed by aiforge scripts/net/set-static-ip-nuc.sh — static IP for the NUC.
network:
  version: 2
  ethernets:
    $IFACE:
      dhcp4: false
      addresses: [$IP/$CIDR]
      routes:
        - to: default
          via: $GW
      nameservers:
        addresses: $DNS_YAML
YAML
chmod 600 "$FILE"
echo "==> netplan generate + apply"
netplan generate
netplan apply
sleep 2
echo "==> now at:"; ip -4 addr show "$IFACE" | grep -w inet || true
echo "==> done. The NUC will keep $IP across reboots. Update your deploy env"
echo "    (AIFORGE_NUC_HOST=$IP) so it never drifts again."
