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

# git: the aiforge-memory dependency installs from a git URL.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# uv for fast, reproducible installs.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_SYSTEM_PYTHON=1 \
    PYTHONUNBUFFERED=1 \
    AIFORGE_CONFIG_DIR=/data/aiforge

# Install deps first (cache layer) — copy only what pip needs to resolve.
COPY pyproject.toml README.md ./
COPY aiforge_core/__init__.py aiforge_core/__init__.py
RUN uv pip install --system -e . || uv pip install --system -e .

# Full source + the pre-built UI.
COPY . .
COPY --from=web /web/dist ./web/dist

RUN mkdir -p /data/aiforge
EXPOSE 8799

CMD ["uvicorn", "aiforge_core.api.api:app", "--host", "0.0.0.0", "--port", "8799"]
