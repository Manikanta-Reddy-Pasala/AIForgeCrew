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
  local label="$1" out="$LA/com.aiforge.$1.plist" ; shift
  local cmd_args=("$@")
  local args_xml=""
  for a in "${cmd_args[@]}"; do
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
    <true/>
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
export PATH="$HOME/.lmstudio/bin:$PATH"
# Wait a moment in case network/filesystem not ready.
sleep 5
lms server start || true
# Load role models @ 128K. -y picks preferred variant automatically.
for kv in "qwen3.6-35b-a3b:" "zai-org/glm-4.7-flash:" "gemma-4-31b-it:"; do
  key="${kv%:*}"
  lms load -y "$key" -c 131072 --gpu max 2>/dev/null || echo "load $key already loaded"
done
# Sleep forever so KeepAlive doesn't thrash.
exec sleep infinity
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
write_plist caffeinate /usr/bin/caffeinate -dimsu
write_plist lmstudio "$LA/com.aiforge.lmstudio.sh"
write_plist paperclip "$LA/com.aiforge.paperclip.sh"
write_plist hermes-dashboard "$LA/com.aiforge.hermes-dashboard.sh"

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
