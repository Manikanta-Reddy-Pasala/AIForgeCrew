#!/usr/bin/env bash
# scripts/claude-mcp-hindsight.sh — wire Hindsight memory into Claude Code CLI.
#
# Hindsight daemon exposes an MCP endpoint at http://127.0.0.1:9177/mcp/.
# Claude Code (used by EM via claude_local adapter) supports MCP servers via
# `claude mcp add`. After this script, EM sessions get hindsight_retain /
# hindsight_recall as tools, sharing the same `aiforge` bank as Hermes agents.
#
# Also raises HINDSIGHT_EMBED_DAEMON_IDLE_TIMEOUT from 300s → 3600s so Claude
# sessions don't hit a stopped daemon between turns.
#
# Idempotent.
set -euo pipefail

[[ "$(uname -s)" == "Darwin" ]] || { echo "claude-mcp-hindsight: macOS only" >&2; exit 1; }

CLAUDE="${CLAUDE:-$HOME/.hermes/node/bin/claude}"
HINDSIGHT_URL="${HINDSIGHT_URL:-http://127.0.0.1:9177/mcp/}"

[[ -x "$CLAUDE" ]] || { echo "Claude CLI missing at $CLAUDE" >&2; exit 1; }

# 1. Raise daemon idle timeout (5m → 1h) in the Hindsight profile env + config.
PROFILE_ENV="$HOME/.hindsight/profiles/hermes.env"
if [[ -f "$PROFILE_ENV" ]]; then
  grep -v "^HINDSIGHT_EMBED_DAEMON_IDLE_TIMEOUT=" "$PROFILE_ENV" > "${PROFILE_ENV}.tmp" || true
  echo "HINDSIGHT_EMBED_DAEMON_IDLE_TIMEOUT=3600" >> "${PROFILE_ENV}.tmp"
  mv "${PROFILE_ENV}.tmp" "$PROFILE_ENV"
  chmod 600 "$PROFILE_ENV"
fi

HS_CFG="$HOME/.hermes/hindsight/config.json"
if [[ -f "$HS_CFG" ]]; then
  python3 - <<PY
import json
from pathlib import Path
p = Path("$HS_CFG")
cfg = json.loads(p.read_text())
cfg["HINDSIGHT_EMBED_DAEMON_IDLE_TIMEOUT"] = 3600
p.write_text(json.dumps(cfg, indent=2))
PY
fi

# 2. Register MCP server with Claude Code (user scope = every session).
# `claude mcp add` is idempotent when the name already exists with the same URL.
"$CLAUDE" mcp remove hindsight --scope user 2>/dev/null || true
"$CLAUDE" mcp add --transport http --scope user hindsight "$HINDSIGHT_URL"

echo
echo "=== verify ==="
"$CLAUDE" mcp list 2>&1 | head -20

echo
echo "Claude Code MCP now includes Hindsight."
echo "EM (claude_local adapter) will see hindsight_retain / hindsight_recall tools."
echo
echo "Daemon URL: $HINDSIGHT_URL"
echo "Daemon idle timeout raised: 300s → 3600s (prevents mid-session disconnects)"
