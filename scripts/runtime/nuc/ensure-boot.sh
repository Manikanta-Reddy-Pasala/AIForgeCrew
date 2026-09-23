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

step() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

step "systemd user units"
mkdir -p "$UNIT_DST"
cp "$UNIT_SRC"/*.service "$UNIT_SRC"/*.timer "$UNIT_DST"/
systemctl --user daemon-reload

step "linger (user units without a login session)"
if command -v loginctl >/dev/null 2>&1; then
  if sudo -n loginctl enable-linger "$USER_NAME" 2>/dev/null; then
    echo "linger enabled for $USER_NAME"
  elif loginctl show-user "$USER_NAME" -p Linger 2>/dev/null | grep -q 'Linger=yes'; then
    echo "linger already on for $USER_NAME"
  else
    echo "WARN: need sudo to enable linger — run:" >&2
    echo "  sudo loginctl enable-linger $USER_NAME" >&2
  fi
else
  echo "WARN: loginctl missing; user units may not start at boot" >&2
fi

step "enable AIForge at boot"
systemctl --user enable aiforge-api.service
systemctl --user enable --now aiforge-api.service
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
if command -v docker >/dev/null 2>&1; then
  if sudo -n systemctl enable --now docker 2>/dev/null; then
    echo "docker enabled"
  elif systemctl is-enabled docker >/dev/null 2>&1; then
    echo "docker already enabled ($(systemctl is-active docker 2>/dev/null || true))"
  else
    echo "WARN: need sudo to enable docker — run: sudo systemctl enable --now docker" >&2
  fi
else
  echo "WARN: docker not on PATH" >&2
fi

step "WireGuard client (tickets.oneshell.in bridge)"
if [[ -f /etc/wireguard/wg0.conf ]]; then
  if sudo -n systemctl enable --now wg-quick@wg0 2>/dev/null; then
    echo "wg-quick@wg0 enabled and started"
    sudo -n wg show wg0 2>/dev/null | head -8 || true
  else
    echo "WARN: need sudo for WireGuard — run:" >&2
    echo "  sudo systemctl enable --now wg-quick@wg0" >&2
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

echo
echo "Boot persistence configured. After reboot, linger + enabled units should"
echo "bring aiforge-api (and docker restart: unless-stopped) back on :8799."
