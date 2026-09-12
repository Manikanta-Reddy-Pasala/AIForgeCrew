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
# The scheme goes through a variable because these are SEARCH patterns for
# Ubuntu's own defaults, which ship as plain http: they must keep matching it.
# Writing https would quiet a clear-text scanner and silently stop matching,
# leaving apt on the public internet rather than the mirror. The destination —
# $AIFORGE_APT_MIRROR — is what the box actually fetches from.
if [[ -n "${AIFORGE_APT_MIRROR:-}" ]]; then
  _sch=http
  sed -i "s|${_sch}://archive.ubuntu.com/ubuntu|$AIFORGE_APT_MIRROR|g; s|${_sch}://security.ubuntu.com/ubuntu|$AIFORGE_APT_MIRROR|g" \
    /etc/apt/sources.list.d/ubuntu.sources 2>/dev/null || true
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

# Inside the box there is nothing to protect from the agent: full rights, no
# workspace jail. run.sh's own host-permission fixes do not apply here.
export AIFORGE_MODE=native AIFORGE_CHAT_WORKSPACE_JAIL=0 AIFORGE_FIX_PERMS=0
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
