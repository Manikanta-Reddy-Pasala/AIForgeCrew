#!/usr/bin/env bash
# Thin wrapper kept at the path the installed systemd unit already points at.
# The work lives in worktree_janitor.py, which reads ticket status through the
# app's own store instead of the Postgres this build no longer has.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
PY="${AIFORGE_PYTHON:-$REPO/.venv/bin/python}"
[[ -x "$PY" ]] || PY="$(command -v python3)"
exec "$PY" "$HERE/worktree_janitor.py" "$@"
