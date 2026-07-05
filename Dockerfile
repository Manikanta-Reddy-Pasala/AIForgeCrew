# AIForge app image — serves the API + UI and runs the graph runner.
# Multi-stage: node builds the web UI, python runtime serves dist/ + API.

# ── web build ─────────────────────────────────────────────────────────
FROM node:20-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

# ── python runtime ────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# git/curl for the runtime (worktrees, git_pr, gh). aiforge-memory is now
# vendored in packages/aiforge_memory/ (no longer a git-URL dependency).
RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# /workspace and per-ticket worktrees are host bind mounts (docker-compose.yml)
# owned by the HOST user, not this container's (root). Git's ownership-safety
# check then refuses every git command run against them ("detected dubious
# ownership") — caught by broad except-blocks upstream, so it fails SILENT:
# the chat "Changes" diff never renders and the post-edit integration-test
# step never fires, with no error surfaced anywhere. Trust every repo this
# container touches; it's the same trust boundary as the shell/file tools
# the API already exposes over HTTP.
RUN git config --system --add safe.directory '*'

# Every subtask-branch commit + merge in parallel_subtasks.py's isolated-
# worktree pipeline (and the parallel-run baseline commit) goes through this
# repo's `_git()` helper, which never checks the exit code — so with no git
# identity configured anywhere, EVERY `git commit` in the container has been
# failing with "Author identity unknown" and getting silently swallowed by
# the same broad except-blocks. The subtask's file writes still land (that
# part isn't git-mediated), so the pipeline reports success — but the
# worktree/merge/diff model that success message describes never actually
# ran: HEAD never advances, so the post-run Changes diff (which compares
# start_sha..HEAD) sees no commits and renders nothing. A bot identity is a
# reasonable default for a container-internal git config that's invisible to
# any real author; override per-committer at the call site if that's ever
# needed.
RUN git config --system user.email "aiforge@localhost" \
    && git config --system user.name "AIForge Bot"

# uv for fast, reproducible installs.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_SYSTEM_PYTHON=1 \
    PYTHONUNBUFFERED=1 \
    AIFORGE_CONFIG_DIR=/data/aiforge

# Full source + the pre-built UI, then install the package (editable).
COPY . .
COPY --from=web /web/dist ./web/dist
# Install the vendored aiforge-memory first (editable) so the Crew install
# below finds the dependency satisfied locally rather than reaching for git.
RUN uv pip install --system -e ./packages/aiforge_memory
RUN uv pip install --system -e .
# Common dev tools so chat sessions can run/test the code they build
# (the agent can pip-install anything else on demand).
RUN uv pip install --system pytest ruff

RUN mkdir -p /data/aiforge
EXPOSE 8799

# SECURITY: this control plane runs shell + edits files over HTTP. Bind
# LOOPBACK by default so the container never exposes it on the LAN by
# accident. To reach it from another host, set BOTH:
#   AIFORGE_BIND_HOST=0.0.0.0  AND  AIFORGE_API_TOKEN=<shared-secret>
# The app itself REFUSES TO BOOT on a non-loopback bind without a token.
# Shell-form CMD so ${AIFORGE_BIND_HOST} expands at runtime; the app reads
# the same var to decide whether auth is mandatory.
CMD uvicorn aiforge_core.api.api:app --host "${AIFORGE_BIND_HOST:-127.0.0.1}" --port "${AIFORGE_PORT:-8799}"
