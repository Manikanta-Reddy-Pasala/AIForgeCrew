#!/bin/sh
# The egress proxy's entrypoint (`./run.sh --isolated`): tinyproxy, re-reading
# its allowlist whenever the egress controller writes a new one.
#
# Waits for the first list instead of starting without one (tinyproxy will not
# start on a missing filter file, and the box must never get a proxy with no
# filter). Reload is by polling the file's change time — no shared process
# namespace with the controller, so either can restart alone.
set -u
ALLOW=/policy/allow.txt
while [ ! -f "$ALLOW" ]; do sleep 1; done
CONF=/etc/tinyproxy/tinyproxy.conf
# On the host's network (the guarded default) the proxy must listen on
# loopback only, on its own port: the shape is the same file, with Port and
# Listen replaced (written to /tmp — the image itself is read-only).
if [ -n "${EGRESS_LISTEN:-}" ]; then
  sed -e "s/^Port .*/Port ${EGRESS_LISTEN##*:}/" -e "s/^Listen .*/Listen ${EGRESS_LISTEN%:*}/" \
    "$CONF" > /tmp/tinyproxy.conf
  CONF=/tmp/tinyproxy.conf
fi
tinyproxy -d -c "$CONF" &
pid=$!
trap 'kill "$pid" 2>/dev/null; exit 0' TERM INT
last="$(stat -c %i:%Y:%s "$ALLOW" 2>/dev/null)"
while kill -0 "$pid" 2>/dev/null; do
  sleep 1
  now="$(stat -c %i:%Y:%s "$ALLOW" 2>/dev/null)"
  if [ "$now" != "$last" ]; then
    last="$now"
    kill -USR1 "$pid" 2>/dev/null   # tinyproxy: reload config + filter
  fi
done
wait "$pid"
