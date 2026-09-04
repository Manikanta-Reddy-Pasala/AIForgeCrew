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
RUN npm ci --ignore-scripts
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

# Builder stage: the repo minus everything .dockerignore excludes (.git, .venv,
# data/, .aiforge/ with its credentials and databases, node_modules, caches and
# now .env / *.pem / *.key). Nothing from this stage reaches the runtime image
# except site-packages, /usr/local/bin and the model cache.
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
# One layer: install the runtime binaries and configure the one of them that
# needs configuring. Split across two RUNs these were two image layers for what
# is a single "make git usable in here" step.
#
# git/curl for worktrees & git_pr (model2vec is pure-numpy — no torch/libgomp).
# tmux: the Doer's bash tool keeps one session per run so `cd` / `export`
# survive between calls — prompts/doer.py promises a "persistent shell". Without
# the binary every command is a fresh subprocess (BashFallback tmux_missing).
# Package names sorted so a future addition lands somewhere findable.
#
# The agent operates on HOST-mounted repos owned by another uid; without
# safe.directory git refuses every command ("dubious ownership") and the failure
# is swallowed upstream (no diff, no post-edit tests). Same trust boundary as
# the shell/file tools the API already exposes over HTTP.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git tmux \
    && rm -rf /var/lib/apt/lists/* \
    && git config --system --add safe.directory '*' \
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
# Named directories, not `COPY . .`. The recursive copy was bounded only by
# .dockerignore — one forgotten pattern (a .env, a key, a scratch dump left in
# the checkout) and it shipped. This lists what the runtime actually needs:
#   aiforge_core/  the app (installed EDITABLE in the builder, so the source
#                  has to be here at the same path)
#   packages/      the vendored aiforge-memory package, likewise editable
#   docker/        the entrypoint this image runs
#   pyproject.toml the editable install's metadata
# Everything else in the repo — tests, docs, installer, services, web sources,
# the .git dir — has no runtime role. .dockerignore still applies on top.
COPY aiforge_core ./aiforge_core
COPY packages ./packages
COPY docker ./docker
COPY pyproject.toml ./
COPY --from=web /web/dist ./web/dist

# ── who the app runs as ───────────────────────────────────────────────────
# Not root. The default `python` image leaves you as uid 0, which means the
# agent's shell, its file edits and anything it installs all run with full
# privileges inside the container — and, on a bind mount, write root-owned
# files onto the host.
#
# The uid is a BUILD ARG because this image mounts host directories (the
# workspace, /data): a container user whose uid does not match the host owner
# cannot write them. Match it to your own (`id -u`) when the default is wrong:
#     docker build --build-arg APP_UID=$(id -u) .
ARG APP_UID=1000
ARG APP_GID=1000
RUN groupadd --gid "$APP_GID" aiforge 2>/dev/null || true \
    && useradd --uid "$APP_UID" --gid "$APP_GID" --create-home aiforge 2>/dev/null || true \
    && mkdir -p /data/aiforge \
    && chmod +x docker/entrypoint.sh \
    && chown -R "$APP_UID:$APP_GID" /data /app 2>/dev/null || true
USER aiforge
EXPOSE 8799

# SECURITY: binds LOOPBACK by default (this control plane runs shell + edits
# files over HTTP). To expose it set BOTH AIFORGE_BIND_HOST=0.0.0.0 AND
# AIFORGE_API_TOKEN=<secret> (the app refuses a non-loopback bind without one).
ENTRYPOINT ["docker/entrypoint.sh"]
