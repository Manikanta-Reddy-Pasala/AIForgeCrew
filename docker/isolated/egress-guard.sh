#!/bin/sh
# The guarded default's firewall (`./run.sh`): the AIForge box shares this
# machine's network, and this keeps ONE rule in place for the box's processes
# only — this machine and the LAN are free, the internet is refused. The
# allowlist proxy and the SSH exit are other containers (other cgroups), so
# what they forward for the box — the hosts on the Settings allowlist — still
# gets out.
#
# Every container of the box (by its compose label: the box and any one-off
# run) is guarded. Every few seconds the rules are checked — the chain's
# contents too, not only the jump — and rebuilt if anything is missing. A
# container restarted under the same id gets a NEW cgroup at the same path; a
# rule keeps pointing at the old one, so a changed cgroup inode re-installs
# the jump. Nothing else on this machine is touched (a chain of our own).
set -u
CHAIN=AIFORGE_EGRESS
LABEL="${AIFORGE_GUARD_LABEL:-aiforge.egress-guarded=1}"
# Free destinations: this machine, private ranges / the LAN, CGNAT (Tailscale).
V4_FREE="127.0.0.0/8 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 100.64.0.0/10 169.254.0.0/16 ${AIFORGE_GUARD_EXTRA_V4:-}"
V6_FREE="::1/128 fc00::/7 fe80::/10 ${AIFORGE_GUARD_EXTRA_V6:-}"
# Never free even inside those ranges: cloud instance metadata (credentials).
V4_DENY="169.254.169.254/32"
STATE=/tmp/guard
mkdir -p "$STATE"

have6() { ip6tables -S OUTPUT >/dev/null 2>&1; }

nameservers() {   # $1 = 4|6 — the host's resolvers (DNS must keep working)
  [ -r /host/resolv.conf ] || return 0
  sed -n 's/^nameserver[[:space:]]*\([^[:space:]]*\).*/\1/p' /host/resolv.conf | while read -r ns; do
    case "$ns" in *:*) [ "$1" = 6 ] && echo "$ns" ;; *) [ "$1" = 4 ] && echo "$ns" ;; esac
  done
}

fill() {   # $1 = ipt, $2 = chain, $3 = free ranges, $4 = deny ranges, $5 = 4|6
  ipt="$1"; c="$2"
  # Replies of connections a remote peer opened (the UI reached from outside).
  $ipt -A "$c" -m conntrack --ctdir REPLY -j RETURN
  for n in $4; do $ipt -A "$c" -d "$n" -j REJECT; done
  for ns in $(nameservers "$5"); do
    $ipt -A "$c" -d "$ns" -p udp --dport 53 -j RETURN
    $ipt -A "$c" -d "$ns" -p tcp --dport 53 -j RETURN
  done
  for n in $3; do $ipt -A "$c" -d "$n" -j RETURN; done
  $ipt -A "$c" -p tcp -j REJECT --reject-with tcp-reset
  $ipt -A "$c" -j REJECT
}

chain_ok() {   # $1 = ipt — the chain ends in both refusals
  $1 -C "$CHAIN" -p tcp -j REJECT --reject-with tcp-reset 2>/dev/null \
    && $1 -C "$CHAIN" -j REJECT 2>/dev/null
}

build() {   # $1 = ipt, $2 = free, $3 = deny, $4 = 4|6 — never leaves the live chain open:
  ipt="$1"  # the new rules go into a fresh chain first, then the jumps are moved
  $ipt -N "$CHAIN" 2>/dev/null || true
  if chain_ok "$ipt" && [ "$(cat "$STATE/built$4" 2>/dev/null)" = "$2|$3" ]; then return 0; fi
  tmp="${CHAIN}_NEW"
  $ipt -N "$tmp" 2>/dev/null || $ipt -F "$tmp"
  fill "$ipt" "$tmp" "$2" "$3" "$4" || return 1
  # Point every jump at the fresh chain, then refill the main one and move back.
  $ipt -S OUTPUT | grep -- "-j $CHAIN\$" | while read -r line; do
    p="$(echo "$line" | sed -n 's/.*--path "\{0,1\}\([^" ]*\)"\{0,1\} .*/\1/p')"
    [ -n "$p" ] && $ipt -I OUTPUT 1 -m cgroup --path "$p" -j "$tmp"
  done
  $ipt -F "$CHAIN" && fill "$ipt" "$CHAIN" "$2" "$3" "$4" || return 1
  $ipt -S OUTPUT | grep -- "-j $tmp\$" | while read -r line; do
    p="$(echo "$line" | sed -n 's/.*--path "\{0,1\}\([^" ]*\)"\{0,1\} .*/\1/p')"
    [ -n "$p" ] && $ipt -D OUTPUT -m cgroup --path "$p" -j "$tmp"
  done
  $ipt -F "$tmp"; $ipt -X "$tmp" 2>/dev/null || true
  echo "$2|$3" > "$STATE/built$4"
}

cgpath() {   # $1 = container id → its cgroup path (systemd driver, else cgroupfs)
  if [ -d "/sys/fs/cgroup/system.slice/docker-$1.scope" ]; then
    echo "/system.slice/docker-$1.scope"
  else
    echo "/docker/$1"
  fi
}

sync_jumps() {   # $1 = ipt, $2 = 4|6, $3.. = cgroup paths to guard (others removed)
  ipt="$1"; v="$2"; shift 2
  $ipt -S OUTPUT | grep -- "-j $CHAIN\$" | while read -r line; do
    p="$(echo "$line" | sed -n 's/.*--path "\{0,1\}\([^" ]*\)"\{0,1\} .*/\1/p')"
    keep=0; for q in "$@"; do [ "$p" = "$q" ] && keep=1; done
    [ "$keep" = 1 ] || { [ -n "$p" ] && $ipt -D OUTPUT -m cgroup --path "$p" -j "$CHAIN" 2>/dev/null; }
  done
  for p in "$@"; do
    ino="$(stat -c %i "/sys/fs/cgroup$p" 2>/dev/null || echo none)"
    key="$STATE/ino$v$(echo "$p" | tr '/' '_')"
    if [ "$(cat "$key" 2>/dev/null)" != "$ino" ]; then
      # A new cgroup at this path (restart): drop the old jump, which still
      # points at the cgroup that is gone, and insert a fresh one.
      while $ipt -D OUTPUT -m cgroup --path "$p" -j "$CHAIN" 2>/dev/null; do :; done
      echo "$ino" > "$key"
    fi
    $ipt -C OUTPUT -m cgroup --path "$p" -j "$CHAIN" 2>/dev/null \
      || $ipt -I OUTPUT 1 -m cgroup --path "$p" -j "$CHAIN"
  done
}

cleanup() {   # stopping the guard (leaving guarded mode): take our rules away —
  # but only when no box container still runs: a guard stopped on its own
  # must not leave a running box unguarded (the rules stay; it is refused).
  running="$(curl -s --max-time 5 --unix-socket /var/run/docker.sock \
             "http://localhost/containers/json?filters=$FILTER" 2>/dev/null | grep -c '"Id"')"
  if [ "${running:-1}" != 0 ]; then
    echo "egress-guard: a box container still runs — keeping its rules" >&2
    exit 0
  fi
  for ipt in iptables ip6tables; do
    $ipt -S OUTPUT 2>/dev/null | grep -- "-j $CHAIN\$" | while read -r line; do
      p="$(echo "$line" | sed -n 's/.*--path "\{0,1\}\([^" ]*\)"\{0,1\} .*/\1/p')"
      [ -n "$p" ] && $ipt -D OUTPUT -m cgroup --path "$p" -j "$CHAIN" 2>/dev/null
    done
    $ipt -F "$CHAIN" 2>/dev/null; $ipt -X "$CHAIN" 2>/dev/null
  done
  exit 0
}
FILTER="$(printf '{"label":["%s"]}' "$LABEL" | sed 's/"/%22/g; s/{/%7B/g; s/}/%7D/g; s/\[/%5B/g; s/\]/%5D/g; s/:/%3A/g; s/,/%2C/g; s/=/%3D/g')"
[ "${AIFORGE_GUARD_CLEANUP_ON_STOP:-1}" = 1 ] && trap cleanup TERM INT
echo "egress-guard: starting"
last=""
while :; do
  ok=1
  build iptables "$V4_FREE" "$V4_DENY" 4 || ok=0
  if have6; then build ip6tables "$V6_FREE" "" 6 || ok=0; fi
  chain_ok iptables || ok=0
  if have6; then chain_ok ip6tables || ok=0; fi
  ids="$(curl -s --max-time 5 --unix-socket /var/run/docker.sock \
         "http://localhost/containers/json?filters=$FILTER" 2>/dev/null \
         | tr ',' '\n' | sed -n 's/^.*"Id":"\([0-9a-f]*\)".*/\1/p')" || true
  paths=""; for id in $ids; do paths="$paths $(cgpath "$id")"; done
  # shellcheck disable=SC2086
  sync_jumps iptables 4 $paths
  # shellcheck disable=SC2086
  if have6; then sync_jumps ip6tables 6 $paths; fi
  for p in $paths; do
    iptables -C OUTPUT -m cgroup --path "$p" -j "$CHAIN" 2>/dev/null || ok=0
    if have6; then ip6tables -C OUTPUT -m cgroup --path "$p" -j "$CHAIN" 2>/dev/null || ok=0; fi
  done
  # Healthy = the rules are complete and every running box container is
  # covered (none running is fine: the box waits for this itself).
  if [ "$ok" = 1 ]; then
    touch /tmp/guarding
    [ "$ids" != "$last" ] && echo "egress-guard: guarding $(echo $ids | wc -w) container(s)" && last="$ids"
  else
    rm -f /tmp/guarding
    echo "egress-guard: rules incomplete — rebuilding" >&2
  fi
  sleep "${AIFORGE_GUARD_INTERVAL:-2}" &
  wait $!
done
