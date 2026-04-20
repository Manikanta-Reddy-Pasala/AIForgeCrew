#!/usr/bin/env bash
# scripts/autostart-install.sh — install macOS LaunchAgents so Paperclip,
# LM Studio server + models, Hermes dashboard, and caffeinate all come up
# on login automatically. LaunchAgents run inside the user's GUI session
# so they inherit the login keychain (Claude Code subscription access).
#
# Agents installed into ~/Library/LaunchAgents/com.aiforge.*.plist
# and loaded via `launchctl bootstrap gui/$(id -u)`.
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "autostart-install: macOS only" >&2; exit 1
fi

LA="$HOME/Library/LaunchAgents"
LOG_DIR="$HOME/aiforge-logs"
REPO="$HOME/AIForgeCrew"
UID_=$(id -u)

mkdir -p "$LA" "$LOG_DIR"

write_plist() {
  local label="$1" ; shift
  local keep_alive="$1" ; shift        # true or false
  local out="$LA/com.aiforge.${label}.plist"
  local args_xml=""
  for a in "$@"; do
    args_xml+="        <string>${a//&/&amp;}</string>\n"
  done
  cat > "$out" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.aiforge.${label}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <${keep_alive}/>
    <key>ProgramArguments</key>
    <array>
$(printf "$args_xml")    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$HOME/.hermes/node/bin:$HOME/.local/bin:$HOME/.lmstudio/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>HOME</key>
        <string>$HOME</string>
    </dict>
    <key>StandardOutPath</key>
    <string>$LOG_DIR/${label}.out.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/${label}.err.log</string>
    <key>ProcessType</key>
    <string>Background</string>
</dict>
</plist>
PLIST
  echo "wrote: $out"
}

# Wrapper script for LM Studio — server + 3 models @ 128K
cat > "$LA/com.aiforge.lmstudio.sh" <<'SH'
#!/usr/bin/env bash
# Oneshot loader — exits 0 after loading. Paired KeepAlive=false in plist.
# Context = 32K per role: 3 models × 32K fits in 96 GB; 128K per model would
# hit the LM Studio guardrail ("insufficient system resources") on the third.
export PATH="$HOME/.lmstudio/bin:$PATH"
sleep 5
lms server start || true
# Current picks (all <3 months old):
#   Sr Dev    → qwen3.6-35b-a3b     (Qwen,   19 GB,  2026-04-14)
#   Tester    → qwen3.5-9b          (Qwen,    6 GB,  2026-03-02)
#   Sr Arch   → gemma-4-26b-a4b-it  (Gemma, 15.6 GB, 2026-04-02)
#
# 64K ctx is the FLOOR — Hermes Agent enforces minimum 64K and refuses to
# initialize sessions on anything smaller ("Failed to initialize agent:
# Model X has a context window of 32,768 tokens, which is below the
# minimum 64,000 required by Hermes Agent").
#
# Memory footprint at 64K (within 96 GB budget):
#   qwen3.6-35b-a3b  ~22 GB  (20 weights + 2 KV)
#   qwen3.5-9b-mlx    ~7 GB  (6 + 1)
#   gemma-4-26b-a4b  ~18 GB  (15.6 + 2)
#   TOTAL            ~47 GB  (+ ~15 GB for Paperclip/Hindsight/system)
for key in "qwen3.6-35b-a3b" "qwen3.5-9b" "gemma-4-26b-a4b-it"; do
  lms load -y "$key" -c 65536 --gpu max 2>/dev/null || echo "load $key already loaded"
done

# Refresh Hermes context_length_cache so Hermes sees the 64K — otherwise a
# stale 32K entry causes "below the minimum 64,000" init errors on first run.
cat > "$HOME/.hermes/context_length_cache.yaml" <<EOF
context_lengths:
  qwen3.6-35b-a3b@http://localhost:1234/v1: 65536
  qwen3.5-9b-mlx@http://localhost:1234/v1: 65536
  gemma-4-26b-a4b-it@http://localhost:1234/v1: 65536
EOF
SH
chmod +x "$LA/com.aiforge.lmstudio.sh"

# Wrapper for Paperclip — starts via npx (same path we use manually).
cat > "$LA/com.aiforge.paperclip.sh" <<'SH'
#!/usr/bin/env bash
export PATH="$HOME/.hermes/node/bin:$HOME/.local/bin:$PATH"
# Small delay so LM Studio comes up first.
sleep 15
exec npx -y paperclipai run
SH
chmod +x "$LA/com.aiforge.paperclip.sh"

# Wrapper for Hermes dashboard — long-running web UI on :9119.
cat > "$LA/com.aiforge.hermes-dashboard.sh" <<'SH'
#!/usr/bin/env bash
export PATH="$HOME/.local/bin:$HOME/.hermes/node/bin:$PATH"
sleep 10
exec hermes dashboard --port 9119 --no-open
SH
chmod +x "$LA/com.aiforge.hermes-dashboard.sh"

# Write plists referencing the wrapper scripts.
# KeepAlive=true for long-running servers, false for one-shot loaders.
write_plist caffeinate       true  /usr/bin/caffeinate -dimsu
write_plist lmstudio         false "$LA/com.aiforge.lmstudio.sh"
write_plist paperclip        true  "$LA/com.aiforge.paperclip.sh"
write_plist hermes-dashboard true  "$LA/com.aiforge.hermes-dashboard.sh"

# Load/reload each agent.
for svc in caffeinate lmstudio paperclip hermes-dashboard; do
  plist="$LA/com.aiforge.${svc}.plist"
  label="com.aiforge.${svc}"
  launchctl bootout "gui/$UID_/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$UID_" "$plist"
  launchctl enable "gui/$UID_/$label"
  echo "loaded: $label"
done

echo
echo "All services auto-start on login. Check with:"
echo "  launchctl list | grep com.aiforge"
echo "Logs: $LOG_DIR/*.{out,err}.log"
