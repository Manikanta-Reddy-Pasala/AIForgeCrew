#!/usr/bin/env bash
# ensure-boot: make AIForge (and WireGuard) come back after every NUC reboot.
#
# Run once on the NUC (or via deploy.sh). Idempotent.
#
# Why this exists: deploy.sh used to restart aiforge-api but never
# `systemctl --user enable`d it, and user units do not start at boot unless
# linger is on. After a reboot the sandbox was gone, WireGuard peers could not
# reach :8799, and Cloudflare returned 502 for tickets.oneshell.in.
set -euo pipefail

CREW="${AIFORGE_CREW_DIR:-$HOME/AIForgeCrew}"
UNIT_SRC="$CREW/scripts/runtime/nuc"
UNIT_DST="$HOME/.config/systemd/user"
USER_NAME="$(id -un)"
fail=0

step() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
need_sudo() {
  echo "FAIL: $1" >&2
  echo "  $2" >&2
  fail=1
}

step "systemd user units"
mkdir -p "$UNIT_DST"
cp "$UNIT_SRC"/*.service "$UNIT_SRC"/*.timer "$UNIT_DST"/
systemctl --user daemon-reload

step "linger (user units without a login session)"
if ! command -v loginctl >/dev/null 2>&1; then
  need_sudo "loginctl missing" "install systemd/logind so user units can start at boot"
elif sudo -n loginctl enable-linger "$USER_NAME" 2>/dev/null; then
  echo "linger enabled for $USER_NAME"
elif loginctl show-user "$USER_NAME" -p Linger 2>/dev/null | grep -q 'Linger=yes'; then
  echo "linger already on for $USER_NAME"
else
  need_sudo "cannot enable linger (need passwordless sudo or root)" \
    "sudo loginctl enable-linger $USER_NAME"
fi

step "enable AIForge at boot"
systemctl --user enable aiforge-api.service
systemctl --user enable --now aiforge-api.service
if ! systemctl --user is-enabled aiforge-api.service >/dev/null 2>&1; then
  need_sudo "aiforge-api.service is not enabled" \
    "systemctl --user enable --now aiforge-api.service"
fi
for svc in aiforge-embed-sidecar aiforge-rerank-sidecar; do
  if [[ -f "$UNIT_DST/$svc.service" ]]; then
    systemctl --user enable "$svc.service" 2>/dev/null \
      && systemctl --user start "$svc.service" 2>/dev/null \
      && echo "enabled $svc" \
      || echo "note: $svc not started (ok if model files absent)"
  fi
done

step "enable timers"
for t in aiforge-git-pull.timer aiforge-repo-pull.timer \
         aiforge-memory-decay.timer aiforge-pr-comments.timer \
         aiforge-worktree-janitor.timer aiforge-lms-ensure.timer; do
  if [[ -f "$UNIT_DST/$t" ]]; then
    systemctl --user enable --now "$t" 2>/dev/null \
      && echo "enabled $t" || echo "WARN: enable failed: $t"
  fi
done

step "docker at boot"
if ! command -v docker >/dev/null 2>&1; then
  need_sudo "docker not on PATH" "install docker and re-run ensure-boot.sh"
elif sudo -n systemctl enable --now docker 2>/dev/null; then
  echo "docker enabled"
elif systemctl is-enabled docker >/dev/null 2>&1; then
  echo "docker already enabled ($(systemctl is-active docker 2>/dev/null || true))"
else
  need_sudo "cannot enable docker at boot" \
    "sudo systemctl enable --now docker"
fi

step "WireGuard client (tickets.oneshell.in bridge)"
if [[ -f /etc/wireguard/wg0.conf ]]; then
  if sudo -n systemctl enable --now wg-quick@wg0 2>/dev/null; then
    echo "wg-quick@wg0 enabled and started"
    sudo -n wg show wg0 2>/dev/null | head -8 || true
  elif systemctl is-enabled wg-quick@wg0 >/dev/null 2>&1 \
      && systemctl is-active wg-quick@wg0 >/dev/null 2>&1; then
    echo "wg-quick@wg0 already enabled and active"
  else
    need_sudo "cannot enable/start wg-quick@wg0" \
      "sudo systemctl enable --now wg-quick@wg0"
  fi
else
  echo "note: /etc/wireguard/wg0.conf missing — install via install-wireguard.sh"
fi

step "status"
systemctl --user is-enabled aiforge-api.service || true
systemctl --user is-active aiforge-api.service || true
loginctl show-user "$USER_NAME" -p Linger 2>/dev/null || true
docker inspect -f '{{.State.Status}}' aiforge 2>/dev/null || echo "aiforge container: not created yet"
curl -fsS -m 5 http://127.0.0.1:8799/api/health 2>/dev/null \
  && echo "OK: /api/health" \
  || echo "WARN: /api/health not answering yet (first start can take minutes)"

if (( fail )); then
  echo >&2
  echo "FAIL: boot persistence is incomplete — fix the commands above, then re-run." >&2
  echo "      Without linger/docker/WG, a reboot will 502 tickets.oneshell.in again." >&2
  exit 1
fi

echo
echo "Boot persistence configured. After reboot, linger + enabled units should"
echo "bring aiforge-api (and docker restart: unless-stopped) back on :8799."
