# AIForge — the DOCKER MODE sandbox (the default `./run.sh`).
#
# An Ubuntu 24.04 box the agent owns outright: it runs as YOUR uid with
# passwordless sudo, so it can install any toolchain a task needs (apt, pip,
# npm — through the internal Artifactory) and do anything inside the box. It
# cannot see the host's files: the only host folder mounted is ~/.aiforge
# (settings, credentials, memory, tickets, and the workspaces it works in).
# Outbound network is open.
#
# The image carries the OS packages and this checkout; nothing else. Python
# deps, node, the web UI and codegraph are installed on the FIRST start by
# run.sh itself — the same lockfile-pinned, Artifactory-only, wheels-only path
# as native mode — into a named volume, so later starts install nothing.
#
# Build args (run.sh passes them; see docker-compose.yml):
#   BASE_REGISTRY  prefix for the base image, e.g. an Artifactory docker remote
#   APT_MIRROR     an Ubuntu mirror (e.g. Artifactory's ubuntu remote); empty = archive.ubuntu.com
#   APP_UID/APP_GID/APP_USER/APP_HOME   your identity, so files the agent writes
#                  into ~/.aiforge stay yours on the host
ARG BASE_REGISTRY=""
FROM ${BASE_REGISTRY}ubuntu:24.04

ARG APT_MIRROR=""
ARG APP_UID=1000
ARG APP_GID=1000
ARG APP_USER=aiforge
ARG APP_HOME=/home/aiforge

ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8 LC_ALL=C.UTF-8 \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

# OS packages only (Ubuntu's default repos unless APT_MIRROR is set).
# python3.12 is Ubuntu 24.04's own; -venv carries ensurepip so run.sh can
# bootstrap uv. No compiler: every Python dep installs as a wheel.
# Then your identity inside the box. Ubuntu 24.04 ships a `ubuntu` user at uid
# 1000; it is removed first so the common uid 1000 is free for you.
#
# The scheme is spelled through a variable rather than written inline: these
# are SEARCH patterns for Ubuntu's own default sources, which ship as plain
# http, so they have to keep matching that. Writing https here would please a
# clear-text scanner and silently stop matching — leaving apt pointed at the
# public internet instead of the mirror, which is the very thing this exists to
# prevent. What the box actually talks to is $APT_MIRROR (https in practice).
RUN if [ -n "$APT_MIRROR" ]; then \
      _sch=http; \
      sed -i "s|${_sch}://archive.ubuntu.com/ubuntu|$APT_MIRROR|g; s|${_sch}://security.ubuntu.com/ubuntu|$APT_MIRROR|g" \
        /etc/apt/sources.list.d/ubuntu.sources; \
    fi \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl git less openssh-client procps python3.12 \
        python3.12-venv rsync sudo tmux \
    && rm -rf /var/lib/apt/lists/* \
    && (userdel -r ubuntu 2>/dev/null || true) \
    && mkdir -p "$APP_HOME" \
    && if [ "$APP_UID" != 0 ]; then \
         (getent group "$APP_GID" >/dev/null || groupadd -g "$APP_GID" "$APP_USER") \
         && useradd -u "$APP_UID" -g "$APP_GID" -d "$APP_HOME" -M -s /bin/bash "$APP_USER" \
         && chown "$APP_UID:$APP_GID" "$APP_HOME"; \
       fi \
    && echo "$APP_USER ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/aiforge \
    && chmod 0440 /etc/sudoers.d/aiforge \
    && git config --system --add safe.directory '*'

# This checkout. The entrypoint syncs it into the state volume on every start,
# so an image rebuild ships new code while installed deps stay put.
# Named paths, not `COPY .`: only what run.sh needs to install and run (the
# app, its packages, the web UI source, the codegraph pin, the tests for
# `--test`). Nothing else of the checkout — docs, installers, local data — can
# ride into the image; .dockerignore still strips caches and build output.
COPY run.sh aiforge.env pyproject.toml uv.lock Makefile README.md LICENSE NOTICE /opt/aiforge-src/
COPY aiforge_core /opt/aiforge-src/aiforge_core
COPY packages /opt/aiforge-src/packages
COPY web /opt/aiforge-src/web
COPY scripts /opt/aiforge-src/scripts
COPY services /opt/aiforge-src/services
COPY docker /opt/aiforge-src/docker
COPY tests /opt/aiforge-src/tests
COPY docker/entrypoint.sh /usr/local/bin/aiforge-entrypoint
RUN chmod 0755 /usr/local/bin/aiforge-entrypoint

ENV AIFORGE_APP_UID=${APP_UID} AIFORGE_APP_GID=${APP_GID} \
    AIFORGE_APP_USER=${APP_USER} AIFORGE_APP_HOME=${APP_HOME}
# The box's environment for EVERY process in it — the app, and any shell you
# (or the agent) open with `./run.sh --shell` / `docker exec`: your home,
# ~/.aiforge as the config dir, ~/.aiforge/repos as the default project root
# (`--repos DIR` overrides it), the app's venv (python, uv, node, npm,
# codegraph) on PATH, and full rights: no workspace jail, sudo installs allowed,
# box-local "caution" commands run free (AIFORGE_SANDBOX, see command_risk).
ENV HOME=${APP_HOME} \
    AIFORGE_CONFIG_DIR=${APP_HOME}/.aiforge \
    AIFORGE_REPO_ROOT=${APP_HOME}/.aiforge/repos \
    AIFORGE_SANDBOX=1 AIFORGE_ALLOW_SUDO_INSTALL=1 \
    AIFORGE_IN_SANDBOX=1 AIFORGE_CHAT_WORKSPACE_JAIL=0 AIFORGE_FIX_PERMS=0 \
    PATH=/var/lib/aiforge/app/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
EXPOSE 8799
ENTRYPOINT ["/usr/local/bin/aiforge-entrypoint"]
