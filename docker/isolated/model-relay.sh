#!/bin/sh
# The model relay (`./run.sh --isolated`): the box's ONE way to the model
# endpoint. Listens on the box network and forwards every connection to the
# single upstream run.sh configured — the box cannot point it anywhere else.
# An https endpoint is spoken TLS to here (certificate checked against the
# upstream's own name), so the box talks plain HTTP to the relay.
set -eu
: "${AIFORGE_MODEL_LISTEN:=1234}" "${AIFORGE_MODEL_UPSTREAM:=host.docker.internal:1234}"
if [ "${AIFORGE_MODEL_TLS:-0}" = 1 ]; then
  host="${AIFORGE_MODEL_UPSTREAM%:*}"; host="${host#[}"; host="${host%]}"
  exec socat "TCP-LISTEN:${AIFORGE_MODEL_LISTEN},fork,reuseaddr" \
    "OPENSSL:${AIFORGE_MODEL_UPSTREAM},verify=1,cafile=/etc/ssl/certs/ca-certificates.crt,commonname=${host},snihost=${host}"
fi
exec socat "TCP-LISTEN:${AIFORGE_MODEL_LISTEN},fork,reuseaddr" "TCP:${AIFORGE_MODEL_UPSTREAM}"
