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

exec docker run --rm -i \
  -v "$REPO_ROOT":/src:ro \
  -e PIP_DISABLE_PIP_VERSION_CHECK=1 \
  "$IMAGE" bash -euo pipefail -c '
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends git curl ca-certificates tmux >/dev/null
    curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null
    . "$HOME/.local/bin/env"
    mkdir -p "$(dirname "'"$WORKDIR"'")"
    git clone -q /src "'"$WORKDIR"'"
    cd "'"$WORKDIR"'"
    uv sync --all-extras --dev --frozen >/dev/null
    uv run pytest "$@"
  ' -- "$@"
