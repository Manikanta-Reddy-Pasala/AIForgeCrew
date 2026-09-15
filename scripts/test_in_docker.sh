#!/usr/bin/env bash
# Reproduce the CI test run in a throwaway container — a FRESH environment,
# not your laptop: a clean `git clone` of HEAD (so gitignored artefacts like
# graphify-out/ are absent, exactly as on the runner), `uv sync --frozen`
# from uv.lock, and plain `pytest` so pyproject's addopts decide which
# markers run. Pass extra pytest args through:
#
#   scripts/test_in_docker.sh                 # what CI runs
#   scripts/test_in_docker.sh -m live_tmux    # the tmux-only tests (tmux is
#                                             # installed in the container)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${AIFORGE_TEST_IMAGE:-python:3.12}"
# /builds/architecture/aiforgecrew mirrors the GitLab runner's checkout path.
WORKDIR="/builds/architecture/aiforgecrew"

# UV_DEFAULT_INDEX / UV_INDEX are forwarded so this works off a box that can
# reach the internal Artifactory as well as on one that cannot (pyproject pins
# the internal index as the default; without an override the build requires
# fail on a DNS error before any test runs).
exec docker run --rm -i \
  -v "$REPO_ROOT":/src:ro \
  -e PIP_DISABLE_PIP_VERSION_CHECK=1 \
  -e UV_DEFAULT_INDEX \
  -e UV_INDEX \
  "$IMAGE" bash -euo pipefail -c '
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends git curl ca-certificates tmux >/dev/null
    curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null
    . "$HOME/.local/bin/env"
    mkdir -p "$(dirname "'"$WORKDIR"'")"
    # /src is the host checkout, owned by the host uid; git in the container
    # runs as root and refuses to read a repo it does not own ("dubious
    # ownership"), which failed the clone before it started.
    git config --global --add safe.directory /src
    git config --global --add safe.directory /src/.git
    git clone -q /src "'"$WORKDIR"'"
    cd "'"$WORKDIR"'"
    uv sync --all-extras --dev --frozen >/dev/null
    uv run pytest "$@"
  ' -- "$@"
