#!/bin/sh
# The guarded default's firewall (`./run.sh`): the AIForge box shares this
# machine's network, and this keeps ONE rule in place for the box's processes
# only — this machine and the LAN are free, the internet is refused. The
# allowlist proxy and the SSH exit are other containers (other cgroups), so
# what they forward for the box — the hosts on the Settings allowlist — still
# gets out. Re-applied every few seconds: it follows the box when it is
# recreated, and comes back after a reboot. Nothing else on this machine is
# touched (a chain of our own, jumped to only for the box's cgroup).
set -u
BOX="${AIFORGE_BOX_NAME:-aiforge}"
CHAIN=AIFORGE_EGRESS
V4_FREE="127.0.0.0/8 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 100.64.0.0/10 169.254.0.0/16 ${AIFORGE_GUARD_EXTRA_V4:-}"
V6_FREE="::1/128 fc00::/7 fe80::/10 ${AIFORGE_GUARD_EXTRA_V6:-}"

chain() {   # $1 = iptables|ip6tables, $2 = free ranges
  ipt="$1"
  $ipt -N "$CHAIN" 2>/dev/null || true
  $ipt -F "$CHAIN"
  for n in $2; do $ipt -A "$CHAIN" -d "$n" -j RETURN; done
  $ipt -A "$CHAIN" -p tcp -j REJECT --reject-with tcp-reset
  $ipt -A "$CHAIN" -j REJECT
}

jumps() {   # $1 = ipt, $2 = cgroup path to keep (others are removed)
  ipt="$1"
  $ipt -S OUTPUT | grep -- "-j $CHAIN" | while read -r line; do
    case "$line" in *"--path $2 "*) continue ;; esac
    eval "$ipt $(echo "$line" | sed 's/^-A /-D /')" 2>/dev/null || true
  done
  $ipt -C OUTPUT -m cgroup --path "$2" -j "$CHAIN" 2>/dev/null \
    || $ipt -I OUTPUT 1 -m cgroup --path "$2" -j "$CHAIN"
}

chain iptables "$V4_FREE"
chain ip6tables "$V6_FREE" 2>/dev/null || true
echo "egress-guard: rules ready; watching container '$BOX'"
last=""
while :; do
  id="$(curl -s --max-time 5 --unix-socket /var/run/docker.sock \
        "http://localhost/containers/$BOX/json" 2>/dev/null \
        | sed -n 's/^{"Id":"\([0-9a-f]*\)".*/\1/p')" || true
  if [ -n "$id" ]; then
    # systemd cgroup driver, else cgroupfs
    if [ -d "/sys/fs/cgroup/system.slice/docker-$id.scope" ]; then
      path="/system.slice/docker-$id.scope"
    else
      path="/docker/$id"
    fi
    jumps iptables "$path"
    jumps ip6tables "$path" 2>/dev/null || true
    # Healthy only while the rule is really in place (v4; v6 too when the
    # host has IPv6 tables) — never on the attempt alone.
    if iptables -C OUTPUT -m cgroup --path "$path" -j "$CHAIN" 2>/dev/null \
       && { ! ip6tables -S >/dev/null 2>&1 \
            || ip6tables -C OUTPUT -m cgroup --path "$path" -j "$CHAIN" 2>/dev/null; }; then
      [ "$id" != "$last" ] && echo "egress-guard: guarding $BOX (${id%"${id#????????????}"})" && last="$id"
      touch /tmp/guarding
    else
      rm -f /tmp/guarding
      echo "egress-guard: could NOT install the rule for $BOX ($path)" >&2
    fi
  else
    rm -f /tmp/guarding
  fi
  sleep "${AIFORGE_GUARD_INTERVAL:-3}"
done
