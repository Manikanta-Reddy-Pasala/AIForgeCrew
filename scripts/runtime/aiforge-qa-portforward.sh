#!/usr/bin/env bash
# Holds kubectl port-forwards from the QA cluster onto NUC localhost so a
# locally-spawned PosClientBackend (run inside the doer's worktree by the
# integration step) can talk to QA Mongo/Redis/NATS/MongoDbService/etc.
#
# One process per dep: kubectl port-forward auto-restarts internally if a
# pod restarts. This script restarts the whole bag if any single PF dies,
# so systemd's Restart=always doesn't have to micro-manage each child.
#
# Each port maps 1:1 onto the upstream port to keep Spring config simple
# (env vars only need to swap the host to 127.0.0.1).

set -u
KCFG="${KCFG:-/home/mani/.kube/qa-config}"
K="kubectl --kubeconfig=$KCFG --insecure-skip-tls-verify"

declare -a TARGETS=(
  # ns:resource:local_port:remote_port   — name reflects what Spring expects
  # NOTE: mongos :27017 is held by a separate, persistent kubectl pf
  # (started outside this service); do not double-bind here or our whole
  # bag exits on EADDRINUSE.
  "default:svc/dragonfly:6379:6379"
  "pos:svc/nats-server:4222:4222"
  "default:svc/mongodbservice:8080:8080"
  "default:svc/posservice:8081:8081"
  "default:svc/gatewayservice:9090:9090"
  "default:svc/businessservice:8092:8092"
)

PIDS=()

cleanup() {
  for pid in "${PIDS[@]:-}"; do
    [[ -n "${pid:-}" ]] && kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
  return
}
trap cleanup EXIT TERM INT

start_one() {
  local spec="$1"
  IFS=":" read -r ns res lport rport <<<"$spec"
  echo "[pf] $ns/$res $lport→$rport"
  $K -n "$ns" port-forward "$res" "$lport:$rport" --address 127.0.0.1 \
    >>/home/mani/.aiforge/logs/qa-pf.log 2>&1 &
  PIDS+=($!)
  return
}

mkdir -p /home/mani/.aiforge/logs
echo "[$(date -Is)] aiforge-qa-portforward start" \
  >> /home/mani/.aiforge/logs/qa-pf.log

for spec in "${TARGETS[@]}"; do
  start_one "$spec"
done

# Watchdog: if any child dies, the whole script exits so systemd
# Restart=always rebuilds the bag from scratch.
while true; do
  for pid in "${PIDS[@]}"; do
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "[$(date -Is)] pf pid $pid died; exiting" \
        >> /home/mani/.aiforge/logs/qa-pf.log
      exit 1
    fi
  done
  sleep 5
done
