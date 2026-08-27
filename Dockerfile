# AIForge — single-mode app image (embedded SQLite + scoped-OKR memory).
# Self-contained: the whole app + ALL deps baked in — python deps, aider
# (RepoMap), the model2vec semantic embedder (static embeddings + sqlite-vec,
# NO torch), the structured/crawl/chunking extras, and the pre-built web UI.
# Nothing is fetched at run time. See docker-compose.yml + docker/entrypoint.sh.
#
# SLIM: semantic recall is model2vec (pure-numpy static embeddings) — no torch,
# no CUDA — and a multi-stage build keeps the compiler toolchain out of the
# final image.

# ── web build ─────────────────────────────────────────────────────────
FROM node:20-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

# ── python builder (has the compiler toolchain; discarded) ─────────────
FROM python:3.12-slim AS builder
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential git \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_SYSTEM_PYTHON=1 PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/opt/hf-cache

COPY . .
# Vendored memory pkg, then the Crew + extras. Semantic recall uses model2vec
# (embed-static) — real static embeddings with NO torch, so the image stays
# small (torch alone was ~1GB). structured/crawl/chunking round out the extras.
# aider-chat is a CORE dep. Dev tools so chat sessions can run/test their code.
RUN uv pip install --system -e ./packages/aiforge_memory \
    && uv pip install --system -e '.[embed-static,structured,crawl,chunking]' \
    && uv pip install --system pytest ruff

# Pre-download the model2vec model (~30MB) so recall works fully OFFLINE. Skip
# with --build-arg PREFETCH_EMBED_MODEL=0 (downloads on first use instead).
ARG PREFETCH_EMBED_MODEL=1
ARG EMBED_MODEL=minishlab/potion-base-8M
RUN if [ "$PREFETCH_EMBED_MODEL" = "1" ]; then \
      python -c "from model2vec import StaticModel as S; S.from_pretrained('${EMBED_MODEL}')" \
      && echo "prefetched ${EMBED_MODEL}"; \
    else echo "skipped embed-model prefetch"; fi \
    && find /usr/local/lib/python3.12/site-packages -name '__pycache__' -type d -prune -exec rm -rf {} + \
    && rm -rf /root/.cache/uv /root/.cache/pip

# ── runtime (slim: no compiler, no uv) ─────────────────────────────────
FROM python:3.12-slim AS runtime
# git/curl for worktrees & git_pr (model2vec is pure-numpy — no torch/libgomp).
# tmux: the Doer's bash tool keeps one session per run so `cd` / `export`
# survive between calls — prompts/doer.py promises a "persistent shell". Without
# the binary every command is a fresh subprocess (BashFallback tmux_missing).
RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates tmux \
    && rm -rf /var/lib/apt/lists/*

# The agent operates on HOST-mounted repos owned by another uid; without this
# git refuses every command ("dubious ownership") and the failure is swallowed
# upstream (no diff, no post-edit tests). Same trust boundary as the shell/file
# tools the API already exposes over HTTP.
RUN git config --system --add safe.directory '*' \
    && git config --system user.email "aiforge@localhost" \
    && git config --system user.name "AIForge Bot"

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    AIFORGE_CONFIG_DIR=/data/aiforge \
    AIFORGE_EMBED_BACKEND=model2vec \
    HF_HOME=/opt/hf-cache
# HF_HOME is an IMAGE path (NOT under /data/aiforge — a runtime bind mount that
# would MASK the baked model); the entrypoint flips HF offline on only when the
# model is cached, so a not-baked image can still download on first use.

# Installed python packages + the baked model from the builder; app source + UI.
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --from=builder /opt/hf-cache /opt/hf-cache
COPY . .
COPY --from=web /web/dist ./web/dist

RUN mkdir -p /data/aiforge && chmod +x docker/entrypoint.sh
EXPOSE 8799

# SECURITY: binds LOOPBACK by default (this control plane runs shell + edits
# files over HTTP). To expose it set BOTH AIFORGE_BIND_HOST=0.0.0.0 AND
# AIFORGE_API_TOKEN=<secret> (the app refuses a non-loopback bind without one).
ENTRYPOINT ["docker/entrypoint.sh"]
