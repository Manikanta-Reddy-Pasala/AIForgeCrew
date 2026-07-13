# AIForge — single-mode app image (embedded SQLite + scoped-OKR memory).
# Self-contained: the whole app + ALL dependencies are baked in — python deps,
# aider (RepoMap), the semantic embedder (sentence-transformers + sqlite-vec +
# torch), the structured/crawl/chunking extras, and the pre-built web UI. At run
# time the host filesystem is mounted so the agent works on real repos; nothing
# else is fetched. See docker-compose.yml + docker/entrypoint.sh.

# ── web build ─────────────────────────────────────────────────────────
FROM node:20-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

# ── python runtime ────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# git/curl for the runtime (worktrees, git_pr, gh); build-essential so packages
# with native bits (sqlite-vec, tokenizers) compile if no wheel is available.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates build-essential \
    && rm -rf /var/lib/apt/lists/*

# The agent operates on HOST-mounted repos owned by another uid; without this,
# git's ownership-safety check refuses every command ("dubious ownership") and
# the failure is swallowed upstream (no diff, no post-edit tests). Trust every
# repo this container touches — same trust boundary as the shell/file tools the
# API already exposes over HTTP.
RUN git config --system --add safe.directory '*' \
    && git config --system user.email "aiforge@localhost" \
    && git config --system user.name "AIForge Bot"

# uv for fast, reproducible installs.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_SYSTEM_PYTHON=1 \
    PYTHONUNBUFFERED=1 \
    AIFORGE_CONFIG_DIR=/data/aiforge \
    AIFORGE_EMBED_BACKEND=semantic \
    HF_HOME=/opt/hf-cache
# HF_HOME is an IMAGE path (NOT under /data/aiforge — that's a runtime bind mount
# that would MASK the baked-in model). The entrypoint flips on HF offline mode
# only when the model is actually cached, so a cached model never blocks on an
# HF fetch AND a not-baked image can still download on first use.

# Full source + the pre-built UI.
COPY . .
COPY --from=web /web/dist ./web/dist

# Install the vendored aiforge-memory first (editable) so the Crew install below
# finds it satisfied locally rather than reaching for a git URL.
RUN uv pip install --system -e ./packages/aiforge_memory
# The Crew + EVERY optional extra so nothing is fetched at run time:
#   semantic  → sentence-transformers + sqlite-vec (+ torch, the big one)
#   structured → instructor · crawl → crawl4ai · chunking → chonkie
# aider-chat is already a CORE dependency (RepoMap). Dev tools for chat sessions.
RUN uv pip install --system -e '.[semantic,structured,crawl,chunking]' \
    && uv pip install --system pytest ruff

# Pre-download the semantic embed model so recall works fully OFFLINE (no HF
# fetch on the first message). Skip with --build-arg PREFETCH_EMBED_MODEL=0 for a
# smaller image / an air-gapped build (the model then downloads on first use, or
# stage it into the hf-cache volume).
ARG PREFETCH_EMBED_MODEL=1
ARG EMBED_MODEL=sentence-transformers/all-MiniLM-L6-v2
RUN if [ "$PREFETCH_EMBED_MODEL" = "1" ]; then \
      python -c "from sentence_transformers import SentenceTransformer as S; S('${EMBED_MODEL}')" \
      && echo "prefetched ${EMBED_MODEL}"; \
    else echo "skipped embed-model prefetch"; fi

RUN mkdir -p /data/aiforge && chmod +x docker/entrypoint.sh
EXPOSE 8799

# SECURITY: this control plane runs shell + edits files over HTTP. Bind LOOPBACK
# by default so the container never lands on the LAN unauthenticated. To expose
# it, set BOTH AIFORGE_BIND_HOST=0.0.0.0 AND AIFORGE_API_TOKEN=<secret> (the app
# refuses to boot on a non-loopback bind without a token).
ENTRYPOINT ["docker/entrypoint.sh"]
