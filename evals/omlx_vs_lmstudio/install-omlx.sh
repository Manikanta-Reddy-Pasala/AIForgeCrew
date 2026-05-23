#!/usr/bin/env bash
# Runs ON the Mac Studio. Idempotent.
# 1. Installs omlx CLI via Homebrew.
# 2. Starts omlx serve as a brew service (or detached if brew services not wired).
# 3. Waits for /v1/models to respond.
set -euo pipefail

OMLX_PORT="${OMLX_PORT:-8000}"
OMLX_MODEL_DIR="${OMLX_MODEL_DIR:-$HOME/.lmstudio/models}"
OMLX_LOG="${OMLX_LOG:-$HOME/omlx-eval/omlx.log}"

if ! command -v brew >/dev/null 2>&1; then
  echo "[fatal] Homebrew not found on $(hostname). Install brew first." >&2
  exit 1
fi

# ---- install ----
if command -v omlx >/dev/null 2>&1; then
  echo "[skip-install] omlx already installed: $(omlx --version 2>&1 || echo '(no --version)')"
else
  echo ">>> brew tap jundot/omlx"
  brew tap jundot/omlx https://github.com/jundot/omlx || true
  echo ">>> brew install omlx"
  brew install omlx
  omlx --version || omlx --help | head -5
fi

# ---- serve ----
is_up() {
  curl -sf "http://localhost:${OMLX_PORT}/v1/models" >/dev/null 2>&1
}

if is_up; then
  echo "[skip-serve] omlx already responding on :${OMLX_PORT}"
  exit 0
fi

# Prefer brew services (survives reboot). Fall back to nohup if formula has no service block.
if brew services list 2>/dev/null | grep -qE '^omlx '; then
  echo ">>> brew services restart omlx"
  brew services restart omlx
else
  echo ">>> brew formula has no service; launching omlx serve detached"
  mkdir -p "$(dirname "$OMLX_LOG")"
  nohup omlx serve --port "$OMLX_PORT" --model-dir "$OMLX_MODEL_DIR" >>"$OMLX_LOG" 2>&1 &
  disown || true
  echo "    log: $OMLX_LOG"
fi

# ---- health wait ----
echo ">>> waiting for omlx on :${OMLX_PORT} (90s max)"
for i in $(seq 1 45); do
  if is_up; then
    echo "[ok] omlx up after ${i}x2s"
    curl -sf "http://localhost:${OMLX_PORT}/v1/models" | head -c 400; echo
    exit 0
  fi
  sleep 2
done

echo "[fatal] omlx did not come up on :${OMLX_PORT} within 90s" >&2
[[ -f "$OMLX_LOG" ]] && { echo "--- last 40 lines of $OMLX_LOG ---"; tail -40 "$OMLX_LOG"; }
exit 1
