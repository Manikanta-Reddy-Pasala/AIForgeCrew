#!/usr/bin/env bash
# Driver: runs HERE, drives the Mac Studio via `ssh ms`.
# Usage:
#   ./run-remote.sh install   # one-time: install omlx CLI on MS
#   ./run-remote.sh smoke     # quick sanity for both servers + both models
#   ./run-remote.sh bench     # full A/B/C across (server x model x domain), ~25 min
#
# Assumes ~/.ssh/config has a `Host ms` entry.
# Models are referenced by the names LM Studio and oMLX use locally; override via env if needed.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SSH_HOST="${SSH_HOST:-ms}"
REMOTE_DIR="${REMOTE_DIR:-\$HOME/omlx-eval}"   # expanded on the remote, hence the literal $

# Server endpoints
LMSTUDIO_BASE="${LMSTUDIO_BASE:-http://localhost:1234/v1}"
OMLX_BASE="${OMLX_BASE:-http://localhost:8000/v1}"
OMLX_MODEL_DIR="${OMLX_MODEL_DIR:-\$HOME/.lmstudio/models}"   # try to share LM Studio's MLX store

# Model identifiers (per-server, since model loaders differ)
LM_MODEL_CODER="${LM_MODEL_CODER:-qwen/qwen3-coder-next}"
LM_MODEL_GENERAL="${LM_MODEL_GENERAL:-granite-4.1-30b}"
OMLX_MODEL_CODER="${OMLX_MODEL_CODER:-qwen3-coder-next-4bit}"
OMLX_MODEL_GENERAL="${OMLX_MODEL_GENERAL:-granite-4.1-30b-4bit}"

RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"

cmd="${1:-}"
[[ -z "$cmd" ]] && { sed -n '3,12p' "$0"; exit 1; }

push_repo() {
  echo ">>> pushing harness to $SSH_HOST:$REMOTE_DIR"
  ssh "$SSH_HOST" "mkdir -p $REMOTE_DIR"
  rsync -az --delete \
    --exclude results --exclude __pycache__ --exclude .venv \
    "$HERE/" "$SSH_HOST:$REMOTE_DIR/"
}

ensure_remote_venv() {
  ssh "$SSH_HOST" bash -se <<'EOF'
set -euo pipefail
cd "$HOME/omlx-eval"
if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi
. .venv/bin/activate
pip -q install --upgrade pip
pip -q install httpx
echo "[ok] venv ready"
EOF
}

case "$cmd" in
  install)
    push_repo
    ssh "$SSH_HOST" bash -s < "$HERE/install-omlx.sh"
    ensure_remote_venv
    ;;

  smoke)
    push_repo
    ensure_remote_venv
    echo ">>> ensure both servers up"
    ssh "$SSH_HOST" bash -se <<'EOF'
set -uo pipefail
# LM Studio headless
if command -v lms >/dev/null 2>&1; then
  lms server status 2>/dev/null | grep -q 'running' || lms server start
else
  echo "[warn] lms CLI missing — LM Studio app must be running with server enabled"
fi
# omlx
curl -sf http://localhost:8000/v1/models >/dev/null || {
  echo "[warn] omlx not up; run ./run-remote.sh install first"
}
EOF
    echo ">>> LM Studio smoke (coder)"
    ssh "$SSH_HOST" bash -se <<EOF
set -euo pipefail
cd ~/omlx-eval && . .venv/bin/activate
python bench.py --server lmstudio --base "$LMSTUDIO_BASE" --model "$LM_MODEL_CODER" --domain coder --phase A --skip-warmup --run-id smoke-\$\$ || true
EOF
    echo ">>> oMLX smoke (coder) — make sure omlx is serving"
    ssh "$SSH_HOST" bash -se <<EOF
set -euo pipefail
cd ~/omlx-eval && . .venv/bin/activate
python bench.py --server omlx --base "$OMLX_BASE" --model "$OMLX_MODEL_CODER" --domain coder --phase A --skip-warmup --run-id smoke-\$\$ || true
EOF
    ;;

  bench)
    push_repo
    ensure_remote_venv
    echo ">>> full bench, run_id=$RUN_ID"
    ssh "$SSH_HOST" bash -se <<EOF
set -euo pipefail
cd ~/omlx-eval && . .venv/bin/activate
mkdir -p results/$RUN_ID

run_cell() {
  local server="\$1" base="\$2" model="\$3" domain="\$4" phase="\$5"
  echo "--- \$server | \$model | \$domain | phase \$phase"
  python bench.py --server "\$server" --base "\$base" --model "\$model" \\
      --domain "\$domain" --phase "\$phase" --run-id "$RUN_ID" || echo "[warn] cell failed: \$server \$model \$domain \$phase"
  sleep 5
}

# LM Studio: assumes lms server is up + model can be auto-loaded by name
for phase in A B C; do
  run_cell lmstudio "$LMSTUDIO_BASE" "$LM_MODEL_CODER"   coder   \$phase
  run_cell lmstudio "$LMSTUDIO_BASE" "$LM_MODEL_GENERAL" general \$phase
done

# oMLX: assumes omlx serve is up. If using brew services, this is already running.
for phase in A B C; do
  run_cell omlx "$OMLX_BASE" "$OMLX_MODEL_CODER"   coder   \$phase
  run_cell omlx "$OMLX_BASE" "$OMLX_MODEL_GENERAL" general \$phase
done

echo "[done] artifacts in ~/omlx-eval/results/$RUN_ID"
EOF
    echo ">>> pulling results back"
    mkdir -p "$HERE/results"
    rsync -az "$SSH_HOST:omlx-eval/results/$RUN_ID/" "$HERE/results/$RUN_ID/"
    echo ">>> writing summary table"
    python3 "$HERE/summarize.py" "$HERE/results/$RUN_ID" > "$HERE/results/$RUN_ID/summary.md"
    echo "[done] $HERE/results/$RUN_ID/summary.md"
    ;;

  *) echo "unknown cmd: $cmd"; exit 2 ;;
esac
