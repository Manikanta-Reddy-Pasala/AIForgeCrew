#!/usr/bin/env bash
# Seed external library docs into ~/.aiforge/docs/ via aiforge-maint
# docs ingest. KISS: one library per call, curated URLs, soft-fail
# per library so a single 404 doesn't abort the whole seed.
#
# Usage:
#   ./scripts/runtime/seed-docs.sh              # all libraries
#   ./scripts/runtime/seed-docs.sh spring react # only named ones
#
# Requires the embed sidecar (port 8764). Bails early with a hint
# when the sidecar is unreachable.
set -uo pipefail

VENV_PYTHON="${AIFORGE_VENV_PYTHON:-$HOME/AIForgeCrew/.venv/bin/python}"
EMBED_SIDECAR_URL="${EMBED_SIDECAR_URL:-http://127.0.0.1:8764/health}"

declare -A LIBS=(
  [spring]="https://docs.spring.io/spring-boot/docs/current/reference/html/index.html https://docs.spring.io/spring-boot/docs/current/reference/html/web.html"
  [react]="https://react.dev/learn/thinking-in-react https://react.dev/reference/react/hooks"
  [mongodb]="https://www.mongodb.com/docs/manual/aggregation https://www.mongodb.com/docs/manual/reference/operator/aggregation"
  [kubernetes]="https://kubernetes.io/docs/concepts/workloads/controllers/deployment/ https://kubernetes.io/docs/concepts/services-networking/service/"
  [tekton]="https://tekton.dev/docs/pipelines/pipelines/ https://tekton.dev/docs/pipelines/tasks/"
)

if ! curl -s -o /dev/null --max-time 3 "$EMBED_SIDECAR_URL"; then
  echo "[seed-docs] WARN: embed sidecar at $EMBED_SIDECAR_URL not reachable."
  echo "[seed-docs]       Start it via scripts/install-embed-sidecar.sh"
  echo "[seed-docs]       Continuing anyway — docs_index will skip embedding."
fi

if [ "$#" -eq 0 ]; then
  set -- "${!LIBS[@]}"
fi

for lib in "$@"; do
  urls="${LIBS[$lib]:-}"
  if [ -z "$urls" ]; then
    echo "[seed-docs] unknown library: $lib (have: ${!LIBS[*]})" >&2
    continue
  fi
  echo "[seed-docs] $lib ⇐ $urls"
  "$VENV_PYTHON" -m aiforge_core.runtime.maintenance_cli docs ingest \
    "$lib" $urls || echo "[seed-docs] $lib failed (non-fatal)"
done
