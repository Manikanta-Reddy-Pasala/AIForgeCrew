#!/usr/bin/env bash
# ensure-boot: make AIForge (and WireGuard) come back after every NUC reboot.
#
# Prefer the *system* unit installed by install-system-boot.sh — that path
# needs no graphical login and no linger. Falls back to user units + linger.
#
# First-time on a fresh NUC (password once):
#   sudo bash scripts/runtime/nuc/install-system-boot.sh
# Then this script is passwordless (sudoers.d/aiforge-boot).
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

# Prefer passwordless sudo; if install-system-boot already ran, -n works.
_sudo_n() { sudo -n "$@"; }

step "system boot unit (no login required)"
if [[ -f /etc/systemd/system/aiforge-api.service ]]; then
  echo "found /etc/systemd/system/aiforge-api.service"
elif [[ -f "$UNIT_SRC/install-system-boot.sh" ]]; then
  echo "system unit missing — installing (may ask for sudo password ONCE)…"
  if [[ $EUID -eq 0 ]]; then
    bash "$UNIT_SRC/install-system-boot.sh"
  elif sudo -n true 2>/dev/null; then
    sudo -n bash "$UNIT_SRC/install-system-boot.sh"
  elif [[ -t 0 ]]; then
    sudo bash "$UNIT_SRC/install-system-boot.sh"
  else
    need_sudo "system aiforge-api.service not installed" \
      "sudo bash $UNIT_SRC/install-system-boot.sh"
  fi
fi

step "systemd user units (timers / sidecars)"
mkdir -p "$UNIT_DST"
cp "$UNIT_SRC"/*.service "$UNIT_SRC"/*.timer "$UNIT_DST"/ 2>/dev/null || true
# Do not overwrite with the system template filename.
rm -f "$UNIT_DST"/*.system.service.in 2>/dev/null || true
systemctl --user daemon-reload 2>/dev/null || true

step "linger (user timers without a login session)"
if ! command -v loginctl >/dev/null 2>&1; then
  need_sudo "loginctl missing" "install systemd/logind so user timers can start at boot"
elif _sudo_n loginctl enable-linger "$USER_NAME" 2>/dev/null; then
  echo "linger enabled for $USER_NAME"
elif loginctl show-user "$USER_NAME" -p Linger 2>/dev/null | grep -q 'Linger=yes'; then
  echo "linger already on for $USER_NAME"
else
  need_sudo "cannot enable linger (run install-system-boot.sh once for NOPASSWD)" \
    "sudo bash $UNIT_SRC/install-system-boot.sh"
fi

# docker BEFORE aiforge-api: Requires=docker.service
step "docker at boot"
if ! command -v docker >/dev/null 2>&1; then
  need_sudo "docker not on PATH" "install docker and re-run"
elif _sudo_n systemctl enable --now docker 2>/dev/null; then
  echo "docker enabled"
elif systemctl is-enabled docker >/dev/null 2>&1 \
    && systemctl is-active docker >/dev/null 2>&1; then
  echo "docker already enabled and active"
elif systemctl is-enabled docker >/dev/null 2>&1; then
  if _sudo_n systemctl start docker 2>/dev/null; then
    echo "docker started"
  else
    need_sudo "docker enabled but not running" "sudo systemctl start docker"
  fi
else
  need_sudo "cannot enable docker at boot" \
    "sudo bash $UNIT_SRC/install-system-boot.sh"
fi

step "WireGuard client (tickets.oneshell.in bridge)"
WG_IFACE=""
for i in wg1 wg0; do
  [[ -f "/etc/wireguard/${i}.conf" ]] && { WG_IFACE="$i"; break; }
done
WG_IFACE="${WG_IFACE:-${AIFORGE_WG_IFACE:-wg1}}"
if [[ -f "/etc/wireguard/${WG_IFACE}.conf" ]]; then
  if _sudo_n systemctl enable --now "wg-quick@${WG_IFACE}" 2>/dev/null; then
    echo "wg-quick@${WG_IFACE} enabled and started"
    _sudo_n wg show "$WG_IFACE" 2>/dev/null | head -8 || true
  elif systemctl is-enabled "wg-quick@${WG_IFACE}" >/dev/null 2>&1 \
      && systemctl is-active "wg-quick@${WG_IFACE}" >/dev/null 2>&1; then
    echo "wg-quick@${WG_IFACE} already enabled and active"
  else
    need_sudo "cannot enable/start wg-quick@${WG_IFACE}" \
      "sudo systemctl enable --now wg-quick@${WG_IFACE}"
  fi
else
  need_sudo "/etc/wireguard/${WG_IFACE}.conf missing" \
    "bash $UNIT_SRC/install-wireguard.sh /path/to/${WG_IFACE}.conf"
fi

step "enable AIForge at boot"
if [[ -f /etc/systemd/system/aiforge-api.service ]]; then
  # System unit: boots with multi-user.target — no login.
  systemctl --user disable aiforge-api.service 2>/dev/null || true
  if _sudo_n systemctl enable --now aiforge-api.service 2>/dev/null; then
    echo "system aiforge-api.service enabled and started"
  elif systemctl is-enabled aiforge-api.service >/dev/null 2>&1 \
      && systemctl is-active aiforge-api.service >/dev/null 2>&1; then
    echo "system aiforge-api.service already active"
  else
    need_sudo "cannot start system aiforge-api.service" \
      "sudo systemctl enable --now aiforge-api.service"
  fi
else
  systemctl --user enable aiforge-api.service
  if systemctl is-active docker >/dev/null 2>&1; then
    systemctl --user enable --now aiforge-api.service
  else
    echo "WARN: docker not active — enabled user aiforge-api but not starting" >&2
    fail=1
  fi
  if ! systemctl --user is-enabled aiforge-api.service >/dev/null 2>&1; then
    need_sudo "aiforge-api.service is not enabled" \
      "systemctl --user enable --now aiforge-api.service"
  fi
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

step "status"
if [[ -f /etc/systemd/system/aiforge-api.service ]]; then
  systemctl is-enabled aiforge-api.service || true
  systemctl is-active aiforge-api.service || true
else
  systemctl --user is-enabled aiforge-api.service || true
  systemctl --user is-active aiforge-api.service || true
fi
loginctl show-user "$USER_NAME" -p Linger 2>/dev/null || true
[[ -f /etc/sudoers.d/aiforge-boot ]] && echo "NOPASSWD sudoers: /etc/sudoers.d/aiforge-boot" \
  || echo "note: no /etc/sudoers.d/aiforge-boot — run install-system-boot.sh once"
docker inspect -f '{{.State.Status}}' aiforge 2>/dev/null || echo "aiforge container: not created yet"
curl -fsS -m 5 http://127.0.0.1:8799/api/health 2>/dev/null \
  && echo "OK: /api/health" \
  || echo "WARN: /api/health not answering yet (first start can take minutes)"

if (( fail )); then
  echo >&2
  echo "FAIL: boot persistence is incomplete — fix the commands above, then re-run." >&2
  echo "      First-time fix (password once): sudo bash $UNIT_SRC/install-system-boot.sh" >&2
  exit 1
fi

echo
echo "Boot persistence OK."
echo "  system unit → starts at multi-user.target (no login / no password)"
echo "  sudo -n     → boot commands via /etc/sudoers.d/aiforge-boot"
echo "  auto-login  → greeter (if install-system-boot configured GDM/LightDM/SDDM)"
