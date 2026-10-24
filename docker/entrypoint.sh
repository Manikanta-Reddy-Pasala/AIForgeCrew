#!/usr/bin/env bash
# Entrypoint of the docker-mode sandbox. Runs as root just long enough to
# prepare the box, then becomes YOUR uid and runs this checkout's run.sh in
# native mode — so the container boots exactly what a host install boots: the
# lockfile-pinned install on first start, the API, the ticket runner and the
# memory sync loop.
#
#   ~/.aiforge (host bind mount, same path)  settings, credentials, memory,
#                                           tickets, chat workspaces, repos/
#   --repos DIR (optional, same path)        your own projects folder
#   /var/lib/aiforge (named volume)          the app copy + its .venv, node_modules,
#                                           web/dist, codegraph — survives restarts
set -euo pipefail

APP_UID="${AIFORGE_APP_UID:-1000}"
APP_GID="${AIFORGE_APP_GID:-1000}"
APP_HOME="${AIFORGE_APP_HOME:-/home/aiforge}"
CFG="$APP_HOME/.aiforge"
SEC="$CFG/security"
STATE=/var/lib/aiforge
APP="$STATE/app"

REPOS="${AIFORGE_REPO_ROOT:-$CFG/repos}"   # --repos DIR, else ~/.aiforge/repos
mkdir -p "$CFG" "$CFG/repos" "$APP"

# The internal CA, into the OS trust store: apt, curl, git, pip (truststore),
# uv (UV_SYSTEM_CERTS) and node (NODE_USE_SYSTEM_CA) then all trust it — for
# the agent's own installs too, not only run.sh's.
for ca in "$SEC/ca/custom-ca.pem" "${AIFORGE_CA_BUNDLE:-}"; do
  if [[ -n "$ca" && -r "$ca" ]]; then
    cp "$ca" /usr/local/share/ca-certificates/aiforge-internal.crt
    update-ca-certificates >/dev/null 2>&1 || true
    echo "==> internal CA installed from $ca"
    break
  fi
done

# The agent may apt-install what a task needs; point apt at the mirror too.
# The sources file is REWRITTEN rather than substituted. Rewriting says what the
# box may fetch from; substituting had to carry Ubuntu's own default URLs as
# search patterns, so anything they failed to match stayed pointed at the public
# internet — the opposite of the intent, and invisible when it happened.
if [[ -n "${AIFORGE_APT_MIRROR:-}" ]]; then
  {
    echo 'Types: deb'
    echo "URIs: $AIFORGE_APT_MIRROR"
    echo 'Suites: noble noble-updates noble-backports noble-security'
    echo 'Components: main universe restricted multiverse'
    echo 'Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg'
  } > /etc/apt/sources.list.d/ubuntu.sources 2>/dev/null || true
fi

# Credentials live in ~/.aiforge/security (the one host folder the box sees).
# Link them where every tool looks: pip/uv read ~/.netrc, npm ~/.npmrc, git
# ~/.gitconfig and ~/.ssh.
for pair in netrc:.netrc npmrc:.npmrc gitconfig:.gitconfig ssh:.ssh; do
  src="$SEC/${pair%%:*}"; dst="$APP_HOME/${pair#*:}"
  if [[ -e "$src" && ! -e "$dst" ]]; then ln -s "$src" "$dst"; fi
done

# This checkout → the state volume. Installed deps and build output stay; code
# comes fresh from the image on every start, so a rebuild ships new code.
# --chown: the code lands as your uid directly — no chown -R over a venv of
# thousands of files on every start.
rsync -a --delete --chown="$APP_UID:$APP_GID" \
  --exclude /.venv --exclude /web/node_modules --exclude /web/dist \
  /opt/aiforge-src/ "$APP/"
chown "$APP_UID:$APP_GID" "$STATE" "$APP" "$CFG/repos" 2>/dev/null || true

# Isolated / guarded network: ssh (and scp / rsync / git over ssh) to the
# internet leaves through the SSH exit (docker/ssh-egress.py), via one
# ProxyCommand; localhost — and in guarded mode the LAN — stays direct.
if [[ ( "${AIFORGE_ISOLATED:-0}" == 1 || "${AIFORGE_GUARDED:-0}" == 1 ) && -d /etc/ssh/ssh_config.d ]]; then
  _py="$(command -v python3 || command -v python3.12 || echo python3)"
  cat > /etc/ssh/ssh_config.d/50-aiforge-egress.conf <<SSHCONF
# Written by aiforge-entrypoint (isolated network): ssh goes out via the SSH exit.
Host localhost 127.0.0.1 ::1
  ProxyCommand none
Host *
  ProxyCommand $_py /opt/aiforge-src/docker/ssh-connect.py %h %p
SSHCONF
elif [[ "${AIFORGE_ISOLATED:-0}" == 1 || "${AIFORGE_GUARDED:-0}" == 1 ]]; then
  echo "!! /etc/ssh/ssh_config.d is missing — ssh to internet hosts will not work" >&2
fi
# Isolated: the model "on this machine" (http://127.0.0.1:PORT or
# host.docker.internal:PORT, as the setup page says) is the model relay — the
# box's own loopback forwards there, so the URL set in Settings keeps working.
if [[ "${AIFORGE_ISOLATED:-0}" == 1 && -n "${AIFORGE_MODEL_LISTEN:-}" ]]; then
  _py="${_py:-$(command -v python3 || command -v python3.12 || echo python3)}"
  setpriv --reuid "${APP_UID:-1000}" --regid "${APP_GID:-1000}" --clear-groups \
    "$_py" /opt/aiforge-src/docker/box-loopback-relay.py \
    "$AIFORGE_MODEL_LISTEN" "model:$AIFORGE_MODEL_LISTEN" </dev/null >/dev/null 2>&1 &
fi

# Guarded network: nothing starts until the firewall really keeps this box
# off the internet (it is applied from outside, a moment after the container
# starts — also after a reboot, when nothing orders the containers).
if [[ "${AIFORGE_GUARDED:-0}" == 1 ]]; then
  _py="${_py:-$(command -v python3 || command -v python3.12 || echo python3)}"
  _guarded=0
  for _i in $(seq 1 60); do
    if "$_py" - <<'PY' 2>/dev/null; then _guarded=1; break; fi
import socket, sys
for addr in (("1.1.1.1", 443), ("2606:4700:4700::1111", 443)):
    try:
        socket.create_connection(addr, timeout=2).close()
        sys.exit(1)           # the internet is reachable directly: not guarded yet
    except OSError:
        pass
sys.exit(0)
PY
    sleep 2
  done
  if [[ "$_guarded" != 1 ]]; then
    echo "!! guarded network: this box can still reach the internet directly — not starting." \
         "See: docker logs aiforge-egress-guard" >&2
    exit 1
  fi
fi

# Inside the box there is nothing to protect from the agent: full rights, no
# workspace jail. run.sh's own host-permission fixes do not apply here.
export AIFORGE_IN_SANDBOX=1 AIFORGE_CHAT_WORKSPACE_JAIL=0 AIFORGE_FIX_PERMS=0
# Projects live in ~/.aiforge/repos by default, or in the folder you mounted
# with --repos (clone/create them there, then point a chat or ticket at one);
# unpinned chats get their own ~/.aiforge/chat-workspaces/session-N as before.
export AIFORGE_CONFIG_DIR="$CFG" AIFORGE_REPO_ROOT="$REPOS"
export HOME="$APP_HOME"

cd "$APP"
# shellcheck disable=SC2086  # AIFORGE_RUN_ARGS is run.sh's own flag list
if [[ "$APP_UID" == 0 ]]; then
  exec bash ./run.sh ${AIFORGE_RUN_ARGS:-}
fi
exec setpriv --reuid "$APP_UID" --regid "$APP_GID" --init-groups \
  bash ./run.sh ${AIFORGE_RUN_ARGS:-}
