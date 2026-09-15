#!/usr/bin/env bash
# Deploy AIForge on this box: pull, then restart the sandbox that serves :8799.
#
#   ssh ai@192.168.70.115 'bash -lc "$HOME/AIForgeCrew/scripts/deploy-nuc.sh"'
#
# A LOGIN shell, because the interactive PATH is where docker (and a rootless
# DOCKER_HOST, where one is configured) is set up.
#
# DOCKER MODE ONLY. There is no host mode any more — run.sh's own header says
# "there is no host mode to choose: AIForge always runs in the sandbox" — so
# this script never kills host processes. The version that did (pkill on
# "run.sh --host" and "uvicorn aiforge_core.api") matched nothing that serves
# the API and could only ever have hit something unrelated, and it then called
# run.sh in a way that tries to CREATE the box, which fails with
#   Conflict. The container name "/aiforge" is already in use
# on every box that already has one. The supported restart is `./run.sh --stop`
# (keeps the container and everything installed inside it) followed by
# `./run.sh --port N` (rebuilds the image so the pulled code is in the box),
# or a plain `docker start aiforge` when the checkout did not move.
#
# Idempotent: re-running it on a healthy, up-to-date box does nothing but
# probe health. Everything that can go wrong exits non-zero with the reason.
#
# Knobs (all optional):
#   AIFORGE_REPO          checkout to deploy         (default ~/AIForgeCrew)
#   AIFORGE_PORT          port to serve and probe    (default 8799)
#   AIFORGE_BIND_HOST     bind address               (default 0.0.0.0)
#   AIFORGE_DEPLOY_WAIT   seconds to wait for health (default 300)
#   --force               restart even when the checkout did not move
#   --no-pull             deploy what is already checked out
set -uo pipefail

REPO="${AIFORGE_REPO:-$HOME/AIForgeCrew}"
PORT="${AIFORGE_PORT:-8799}"
# 0.0.0.0, not run.sh's loopback default: this box serves the fleet, and a
# deploy that quietly narrowed the bind would look healthy here and be
# unreachable from every other machine.
HOST="${AIFORGE_BIND_HOST:-0.0.0.0}"
WAIT="${AIFORGE_DEPLOY_WAIT:-300}"
BOX=aiforge                              # docker-compose.yml's container_name
FORCE=0
PULL=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --force) FORCE=1 ;;
    --no-pull) PULL=0 ;;
    -h|--help) sed -n '2,32p' "$0"; exit 0 ;;
    *) echo "!! unknown flag: $1" >&2; exit 2 ;;
  esac
  shift
done

_fatal() { echo "!! $1" >&2; [[ -n "${2:-}" ]] && echo "!! $2" >&2; exit 1; }

cd "$REPO" 2>/dev/null || _fatal "no checkout at $REPO." "Set AIFORGE_REPO to the one to deploy."

# Fail on the missing tool rather than on its first confusing error message.
command -v docker >/dev/null 2>&1 \
  || _fatal "docker is not on PATH — AIForge only runs in its Docker sandbox." \
            "Run this through a login shell: ssh <box> 'bash -lc \"…/deploy-nuc.sh\"'"
docker info >/dev/null 2>&1 \
  || _fatal "cannot talk to the Docker daemon as $(id -un 2>/dev/null || id -u)." \
            "Check the daemon is up, and that this user is in the docker group."

# ── pull ──────────────────────────────────────────────────────────────────
# The box has no bind mount of this checkout (docker-compose.yml mounts only
# ~/.aiforge; the app lives in the aiforge-state volume), so pulled code
# reaches it ONLY through an image rebuild. That is why a moved HEAD forces the
# stop+up path below instead of a cheap `docker start`.
BEFORE="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
if (( PULL )); then
  echo "==> pull"
  if ! _out="$(git pull --ff-only 2>&1)"; then
    echo "$_out" >&2
    _fatal "git pull --ff-only failed, so there is nothing new to deploy." \
           "Local edits (aiforge.env is the usual one) or a diverged branch — resolve, or pass --no-pull to deploy what is checked out."
  fi
  echo "$_out" | tail -1
fi
AFTER="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
echo "==> checkout: ${AFTER:0:8}$([[ "$BEFORE" != "$AFTER" ]] && echo " (was ${BEFORE:0:8})")"

# ── what is on the box now ────────────────────────────────────────────────
# Compose only manages containers it labelled as part of THIS project. A box
# named `aiforge` that came from another directory (or from a bare docker run)
# is invisible to `docker compose stop` and then collides with `up` on the
# name — the exact failure that broke this deploy. Say so instead of handing
# the operator compose's Conflict message.
STATE="$(docker inspect -f '{{.State.Status}}' "$BOX" 2>/dev/null || true)"
if [[ -n "$STATE" ]]; then
  OWNER_DIR="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' "$BOX" 2>/dev/null || true)"
  if [[ -z "$OWNER_DIR" ]]; then
    _fatal "a container named '$BOX' exists but carries no compose labels, so compose here cannot manage it." \
           "Either deploy from wherever it was created, or remove it (docker rm -f $BOX) and accept that anything the agent installed inside it is lost."
  elif [[ "$OWNER_DIR" != "$PWD" ]]; then
    _fatal "the '$BOX' container belongs to the checkout at $OWNER_DIR, not this one ($PWD)." \
           "Deploy from that directory, or remove the container (docker rm -f $BOX) to recreate it from here."
  fi
  echo "==> sandbox: $STATE"
else
  echo "==> sandbox: not created yet"
fi

# ── restart ───────────────────────────────────────────────────────────────
# Three cases, cheapest first. Anything that (re)creates the container bakes
# the environment it is given NOW (compose reads it at creation), which is why
# the index is echoed here: a box created without a reachable UV_DEFAULT_INDEX
# cannot install its dependencies and never answers health.
if [[ "$STATE" == running && "$BEFORE" == "$AFTER" && $FORCE -eq 0 ]]; then
  echo "==> already running the deployed commit — probing health only (--force to restart)"
elif [[ -n "$STATE" && "$BEFORE" == "$AFTER" && $FORCE -eq 0 ]]; then
  # Nothing to rebuild: start the existing box, keeping everything in it.
  echo "==> docker start $BOX"
  docker start "$BOX" >/dev/null || _fatal "docker start $BOX failed — see: docker logs $BOX"
else
  echo "==> stop (the container and everything installed in it survive)"
  ./run.sh --stop || _fatal "./run.sh --stop failed — the box was left as it was."
  # No apostrophe in the default: bash re-parses quotes inside ${x:-…}, and one
  # there opens a string that never closes — the whole script fails to parse.
  echo "==> up on ${HOST}:${PORT} (index: ${UV_DEFAULT_INDEX:-the one pyproject names})"
  ./run.sh --host "$HOST" --port "$PORT" \
    || _fatal "./run.sh could not bring the sandbox up." \
              "Read the error above: a name conflict means a foreign '$BOX' container, an image build failure usually means the box cannot reach its package index."
fi

# ── health ────────────────────────────────────────────────────────────────
# The port is the wrong thing to watch: with network_mode: host the box binds
# it only once the API is actually serving, and a first start installs its
# dependencies first — minutes, not seconds. So poll the endpoint itself.
echo "==> waiting up to ${WAIT}s for /api/health on :$PORT"
deadline=$(( $(date +%s) + WAIT ))
while :; do
  code="$(curl -s -m 6 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/api/health" 2>/dev/null)"
  [[ "$code" == 200 ]] && { echo "OK: health 200 on :$PORT (${AFTER:0:8})"; exit 0; }
  (( $(date +%s) >= deadline )) && break
  # A box that has already died will never answer, so stop waiting on it.
  now="$(docker inspect -f '{{.State.Status}}' "$BOX" 2>/dev/null || echo gone)"
  [[ "$now" == running ]] || { echo "!! sandbox is '$now', not running" >&2; break; }
  sleep 5
done

echo "!! FAIL: /api/health did not answer 200 on :$PORT (last: ${code:-no answer})" >&2
LOGS="$(docker logs --tail 40 "$BOX" 2>&1 || true)"
echo "$LOGS" >&2
# The one failure mode whose fix is not in its own message: compose bakes the
# environment at CREATION, so exporting UV_DEFAULT_INDEX and re-running does
# nothing for a container that already exists — it has to be recreated.
if grep -qi 'does not resolve\|no package index\|could not install uv' <<<"$LOGS"; then
  echo "!! the box cannot reach its package index." >&2
  echo "!! UV_DEFAULT_INDEX is baked in when the container is CREATED: export it, then" >&2
  echo "!!   docker rm -f $BOX && UV_DEFAULT_INDEX=… ./run.sh --host $HOST --port $PORT" >&2
fi
echo "!! follow it live: ./run.sh --logs" >&2
exit 1
