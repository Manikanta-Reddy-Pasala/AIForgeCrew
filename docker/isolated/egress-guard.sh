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

sync_jumps() {   # $1 = ipt, $2.. = cgroup paths to guard (every other jump is removed)
  ipt="$1"; shift
  $ipt -S OUTPUT | grep -- "-j $CHAIN" | while read -r line; do
    keep=0
    for p in "$@"; do case "$line" in *"--path \"$p\" "*|*"--path $p "*) keep=1 ;; esac; done
    [ "$keep" = 1 ] && continue
    p="$(echo "$line" | sed -n 's/.*--path "\{0,1\}\([^" ]*\)"\{0,1\} .*/\1/p')"
    [ -n "$p" ] && $ipt -D OUTPUT -m cgroup --path "$p" -j "$CHAIN" 2>/dev/null || true
  done
  for p in "$@"; do
    $ipt -C OUTPUT -m cgroup --path "$p" -j "$CHAIN" 2>/dev/null \
      || $ipt -I OUTPUT 1 -m cgroup --path "$p" -j "$CHAIN"
  done
}

cgpath() {   # $1 = container id → its cgroup path (systemd driver, else cgroupfs)
  if [ -d "/sys/fs/cgroup/system.slice/docker-$1.scope" ]; then
    echo "/system.slice/docker-$1.scope"
  else
    echo "/docker/$1"
  fi
}

chain iptables "$V4_FREE"
chain ip6tables "$V6_FREE" 2>/dev/null || true
echo "egress-guard: rules ready"
last=""
# Every container of the box's service — the box itself and any one-off
# (`./run.sh --test`, maintenance runs) — by its compose label.
LABEL="${AIFORGE_GUARD_LABEL:-aiforge.egress-guarded=1}"
FILTER="$(printf '{"label":["%s"]}' "$LABEL" | sed 's/"/%22/g; s/{/%7B/g; s/}/%7D/g; s/\[/%5B/g; s/\]/%5D/g; s/:/%3A/g; s/,/%2C/g; s/=/%3D/g')"
last=""
while :; do
  ids="$(curl -s --max-time 5 --unix-socket /var/run/docker.sock \
         "http://localhost/containers/json?filters=$FILTER" 2>/dev/null \
         | tr ',' '\n' | sed -n 's/^.*"Id":"\([0-9a-f]*\)".*/\1/p')" || true
  if [ -n "$ids" ]; then
    paths=""; for id in $ids; do paths="$paths $(cgpath "$id")"; done
    # shellcheck disable=SC2086
    sync_jumps iptables $paths
    # shellcheck disable=SC2086
    sync_jumps ip6tables $paths 2>/dev/null || true
    ok=1
    for p in $paths; do
      iptables -C OUTPUT -m cgroup --path "$p" -j "$CHAIN" 2>/dev/null || ok=0
      if ip6tables -S >/dev/null 2>&1; then
        ip6tables -C OUTPUT -m cgroup --path "$p" -j "$CHAIN" 2>/dev/null || ok=0
      fi
    done
    if [ "$ok" = 1 ]; then
      [ "$ids" != "$last" ] && echo "egress-guard: guarding $(echo $ids | wc -w) container(s)" && last="$ids"
      touch /tmp/guarding
    else
      rm -f /tmp/guarding
      echo "egress-guard: could NOT install the rule for every box container" >&2
    fi
  else
    rm -f /tmp/guarding
  fi
  sleep "${AIFORGE_GUARD_INTERVAL:-3}"
done
