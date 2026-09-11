# AIForge — single-mode app image (embedded SQLite + scoped-OKR memory).
# Self-contained: the whole app + ALL deps baked in — python deps, aider
# (RepoMap), the model2vec semantic embedder (static embeddings + sqlite-vec,
# NO torch), the structured/crawl/chunking extras, and the pre-built web UI.
# Nothing is fetched at run time. See docker-compose.yml + docker/entrypoint.sh.
#
# SLIM: semantic recall is model2vec (pure-numpy static embeddings) — no torch,
# no CUDA — and a multi-stage build keeps the compiler toolchain out of the
# final image.

# Every package comes from the internal Artifactory; nothing falls back to a
# public registry. BASE_REGISTRY prefixes the base images (e.g. an Artifactory
# docker remote, "artifactory.internal/docker-remote/"); empty = Docker Hub.
ARG BASE_REGISTRY=""
ARG NPM_REGISTRY=https://artifactory.internal/artifactory/api/npm/npm-remote/

# ── web build ─────────────────────────────────────────────────────────
FROM ${BASE_REGISTRY}node:20-slim AS web
ARG NPM_REGISTRY
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund --registry "$NPM_REGISTRY"
COPY web/ ./
RUN npm run build

# ── codegraph (the Doer's enforced codegraph_* tools) ───────────────────
# Same lockfile run.sh installs from. Only the per-platform package is kept:
# it carries its own Node, and the package's npm `bin` shim is the thing that
# downloads a bundle from GitHub when that platform package is missing.
FROM ${BASE_REGISTRY}node:20-slim AS codegraph
ARG NPM_REGISTRY
WORKDIR /cg
COPY scripts/codegraph/package.json scripts/codegraph/package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund --registry "$NPM_REGISTRY" \
    && mkdir /out && cp -a node_modules/@colbymchenry/codegraph-linux-* /out/codegraph \
    && /out/codegraph/bin/codegraph --version

# ── python builder (discarded) ─────────────────────────────────────────
# No compiler on purpose: every dependency installs as a wheel, so anything
# that would need building from source fails here instead of quietly compiling.
FROM ${BASE_REGISTRY}python:3.12-slim AS builder
WORKDIR /app
ENV UV_SYSTEM_PYTHON=1 PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/opt/hf-cache

# Named directories, not `COPY . .` — the same rule the runtime stage already
# follows, and for the same reason: a recursive copy of the build context is
# bounded only by .dockerignore, so one forgotten pattern (a .env, a key, a
# scratch dump in the checkout) puts it in a layer. A discarded stage is still
# a layer: it lands in the builder's cache and in anything that pulls it.
#
# Both installs below are EDITABLE, so what they need is exactly the two
# package trees plus the root metadata:
#   pyproject.toml  the root distribution's metadata (no readme/license refs)
#   uv.lock         the versions every install below is pinned to
#   aiforge_core/   the app
#   packages/       the vendored aiforge-memory distribution
COPY pyproject.toml uv.lock ./
COPY aiforge_core ./aiforge_core
COPY packages ./packages
# The Crew + extras. Semantic recall uses model2vec (embed-static) — real static
# embeddings with NO torch, so the image stays small (torch alone was ~1GB).
# structured/crawl/chunking round out the extras; `dev` (pytest, ruff) so chat
# sessions can run/test their code.
# Index: UV_DEFAULT_INDEX if passed, else pyproject's (the estate's
# Artifactory). No public fallback: an unresolvable index fails the build.
# uv: its PyPI wheel at the version uv.lock pins — not the ghcr.io image.
# Versions: exactly uv.lock's for the image's extras (a fresh resolve gave
# starlette 0.52.1, under the CVE floor). --no-config: in /app, uv pip reads
# pyproject's override-dependencies, and an override REPLACES a pin — litellm
# ==1.98.0 became >=1.84.0 and resolved 1.100.1. The pins are --override as
# well as -r: google-adk caps starlette <1, which only an override beats (it is
# how uv.lock got 1.6.0). Wheels only: every dependency
# goes in with --no-build, then this checkout's two packages alone, --no-deps.
ARG UV_DEFAULT_INDEX=""
RUN url="$(sed -n '/^\[\[tool\.uv\.index\]\]/,/^\[/s/^url *= *"\(.*\)"/\1/p' pyproject.toml | head -1)"; \
    idx="${UV_DEFAULT_INDEX:-$url}"; host="$(echo "$idx" | sed 's|^[a-z]*://||; s|[:/].*||')"; \
    getent hosts "$host" >/dev/null \
      || { echo "package index host '$host' does not resolve — this build uses only $idx" >&2; exit 1; }; \
    export UV_DEFAULT_INDEX="$idx"; \
    uvver="$(sed -n '/^name = "uv"$/{n;s/^version = "\(.*\)"/\1/p;}' uv.lock)"; \
    pip install -q --no-cache-dir --disable-pip-version-check --only-binary=:all: \
        --index-url "$idx" "uv==$uvver" \
    && uv export --frozen --no-dev --no-hashes --no-emit-project --no-emit-local --quiet \
         --extra embed-static --extra structured --extra crawl --extra chunking --extra dev \
         -o /tmp/image-pins.txt \
    && uv pip install --system --no-config --no-build \
         -r /tmp/image-pins.txt --override /tmp/image-pins.txt \
    && uv pip install --system --no-deps -e ./packages/aiforge_memory -e . \
    && pip uninstall -q -y uv && rm -f /tmp/image-pins.txt

# Pre-download the model2vec model (~30MB) so recall works fully OFFLINE. Skip
# with --build-arg PREFETCH_EMBED_MODEL=0 (downloads on first use instead).
ARG PREFETCH_EMBED_MODEL=1
ARG EMBED_MODEL=minishlab/potion-base-8M
RUN if [ "$PREFETCH_EMBED_MODEL" = "1" ]; then \
      python -c "from model2vec import StaticModel as S; S.from_pretrained('${EMBED_MODEL}')" \
      && echo "prefetched ${EMBED_MODEL}"; \
    else echo "skipped embed-model prefetch"; fi \
    && mkdir -p "$HF_HOME" \
    && find /usr/local/lib/python3.12/site-packages -name '__pycache__' -type d -prune -exec rm -rf {} + \
    && rm -rf /root/.cache/uv /root/.cache/pip

# ── runtime (slim: no compiler, no uv) ─────────────────────────────────
FROM ${BASE_REGISTRY}python:3.12-slim AS runtime
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
COPY --from=codegraph /out/codegraph /opt/codegraph
RUN ln -s /opt/codegraph/bin/codegraph /usr/local/bin/codegraph
ENV CODEGRAPH_TELEMETRY=0 CODEGRAPH_NO_DOWNLOAD=1

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
