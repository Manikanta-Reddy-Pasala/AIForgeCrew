#!/usr/bin/env bash
# install-system-boot: make AIForge start at boot with NO login and NO password
# prompts for the reboot path.
#
# One interactive sudo (password once) installs everything; after that, reboot
# brings docker + WireGuard + aiforge-api without anyone at the keyboard.
#
# Installs:
#   1. /etc/systemd/system/aiforge-api.service  (system unit, multi-user.target)
#   2. /etc/sudoers.d/aiforge-boot              (NOPASSWD for boot ops)
#   3. optional display-manager AutomaticLogin (GDM / LightDM / SDDM)
#   4. linger + docker group membership
#
# Usage (on NUC, once):
#   sudo bash scripts/runtime/nuc/install-system-boot.sh
#   # or without logging in as root:
#   bash scripts/runtime/nuc/install-system-boot.sh   # will sudo - ask once
set -euo pipefail

CREW="${AIFORGE_CREW_DIR:-$HOME/AIForgeCrew}"
UNIT_SRC="$CREW/scripts/runtime/nuc"
TEMPLATE="$UNIT_SRC/aiforge-api.system.service.in"
TARGET_USER="${AIFORGE_BOOT_USER:-${SUDO_USER:-$(id -un)}}"
if [[ "$TARGET_USER" == root ]]; then
  echo "refuse to install as root — set AIFORGE_BOOT_USER=mani (the NUC login)" >&2
  exit 2
fi
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
[[ -n "$TARGET_HOME" && -d "$TARGET_HOME" ]] \
  || { echo "no home for user $TARGET_USER" >&2; exit 2; }
CREW_DIR="${AIFORGE_CREW_DIR:-$TARGET_HOME/AIForgeCrew}"
[[ -x "$CREW_DIR/run.sh" ]] \
  || { echo "no run.sh at $CREW_DIR — set AIFORGE_CREW_DIR" >&2; exit 2; }
[[ -f "$TEMPLATE" ]] || { echo "missing $TEMPLATE" >&2; exit 2; }

AUTO_LOGIN="${AIFORGE_AUTO_LOGIN:-1}"

_sudo() {
  if [[ $EUID -eq 0 ]]; then
    "$@"
  else
    sudo "$@"
  fi
}

echo "==> target user: $TARGET_USER  home: $TARGET_HOME  crew: $CREW_DIR"

# ── 1. system unit ────────────────────────────────────────────────────────
tmp="$(mktemp)"
sed -e "s|__USER__|$TARGET_USER|g" \
    -e "s|__HOME__|$TARGET_HOME|g" \
    -e "s|__CREW__|$CREW_DIR|g" \
    "$TEMPLATE" > "$tmp"
_sudo install -m 644 "$tmp" /etc/systemd/system/aiforge-api.service
rm -f "$tmp"
_sudo systemctl daemon-reload
_sudo systemctl enable aiforge-api.service
echo "installed /etc/systemd/system/aiforge-api.service (WantedBy=multi-user.target)"

# Prefer the system unit over the user unit so they never fight.
# When this script is run via sudo, `systemctl --user` would hit *root's*
# user manager — target TARGET_USER's lingering session instead.
_uid="$(id -u "$TARGET_USER")"
_runtime="/run/user/${_uid}"
if [[ -d "$_runtime" ]] || _sudo loginctl enable-linger "$TARGET_USER" 2>/dev/null; then
  if _sudo systemctl --user -M "${TARGET_USER}@" disable aiforge-api.service 2>/dev/null \
    || _sudo -u "$TARGET_USER" env XDG_RUNTIME_DIR="$_runtime" \
         systemctl --user disable aiforge-api.service 2>/dev/null; then
    echo "disabled user aiforge-api.service for $TARGET_USER (system unit owns boot)"
  else
    echo "note: user aiforge-api.service not enabled for $TARGET_USER (ok)"
  fi
fi

# ── 2. passwordless sudo for boot ops only ────────────────────────────────
# Fixed system binaries + explicit args — NEVER NOPASSWD on scripts under
# $CREW_DIR (user-writable → trivial root escalation). Never bare systemctl.
SUDOERS=/etc/sudoers.d/aiforge-boot
sudoers_tmp="$(mktemp)"
# Resolve realpaths so both /bin and /usr/bin forms match what sudo -n sees.
_sc="$(command -v systemctl)"
_lc="$(command -v loginctl)"
_wg="$(command -v wg || true)"
_wq="$(command -v wg-quick || true)"
{
  echo "# AIForge NUC boot — managed by scripts/runtime/nuc/install-system-boot.sh"
  echo "# Passwordless ONLY for the listed subcommands (not systemctl/ufw wholesale)."
  echo "Cmnd_Alias AIFORGE_BOOT = \\"
  echo "  ${_sc} enable docker, \\"
  echo "  ${_sc} enable --now docker, \\"
  echo "  ${_sc} start docker, \\"
  echo "  ${_sc} restart docker, \\"
  echo "  ${_sc} enable wg-quick@wg0, \\"
  echo "  ${_sc} enable --now wg-quick@wg0, \\"
  echo "  ${_sc} start wg-quick@wg0, \\"
  echo "  ${_sc} restart wg-quick@wg0, \\"
  echo "  ${_sc} enable aiforge-api.service, \\"
  echo "  ${_sc} enable --now aiforge-api.service, \\"
  echo "  ${_sc} start aiforge-api.service, \\"
  echo "  ${_sc} restart aiforge-api.service, \\"
  echo "  ${_sc} daemon-reload, \\"
  echo "  ${_sc} status aiforge-api.service, \\"
  echo "  ${_lc} enable-linger ${TARGET_USER}"
  if [[ -n "$_wg" ]]; then
    echo "Cmnd_Alias AIFORGE_WG = ${_wg} show, ${_wg} show wg0"
    [[ -n "$_wq" ]] && echo "Cmnd_Alias AIFORGE_WGQ = ${_wq} up wg0, ${_wq} down wg0"
  fi
  echo "$TARGET_USER ALL=(root) NOPASSWD: AIFORGE_BOOT"
  if [[ -n "$_wg" ]]; then
    echo "$TARGET_USER ALL=(root) NOPASSWD: AIFORGE_WG"
    [[ -n "$_wq" ]] && echo "$TARGET_USER ALL=(root) NOPASSWD: AIFORGE_WGQ"
  fi
} > "$sudoers_tmp"
if _sudo visudo -cf "$sudoers_tmp" >/dev/null 2>&1; then
  _sudo install -m 440 "$sudoers_tmp" "$SUDOERS"
  echo "installed $SUDOERS (NOPASSWD for listed boot commands only)"
else
  echo "FAIL: generated sudoers failed visudo -c — not installing" >&2
  cat "$sudoers_tmp" >&2
  rm -f "$sudoers_tmp"
  exit 1
fi
rm -f "$sudoers_tmp"

# ── 3. docker group + linger ──────────────────────────────────────────────
if getent group docker >/dev/null 2>&1; then
  _sudo usermod -aG docker "$TARGET_USER" || true
  echo "added $TARGET_USER to docker group (re-login once if docker still denied)"
fi
_sudo loginctl enable-linger "$TARGET_USER"
echo "linger enabled for $TARGET_USER (user timers still work)"

# ── 4. docker + WireGuard enable ──────────────────────────────────────────
_sudo systemctl enable --now docker
if [[ -f /etc/wireguard/wg0.conf ]]; then
  _sudo systemctl enable --now wg-quick@wg0
  echo "wg-quick@wg0 enabled"
else
  echo "WARN: /etc/wireguard/wg0.conf missing — install via install-wireguard.sh" >&2
fi

# ── 5. optional display auto-login (no password at the greeter) ───────────
if [[ "$AUTO_LOGIN" == "1" ]]; then
  if [[ -d /etc/gdm3 ]]; then
    conf=/etc/gdm3/custom.conf
    _sudo mkdir -p /etc/gdm3
    if [[ -f "$conf" ]]; then
      _sudo cp -a "$conf" "$conf.bak.$(date +%s)"
    else
      printf '%s\n' '[daemon]' '[security]' '[xdmcp]' '[chooser]' '[debug]' \
        | _sudo tee "$conf" >/dev/null
    fi
    gdm_tmp="$(mktemp)"
    # Write to a temp file first — never tee onto the same path python reads.
    python3 - "$conf" "$TARGET_USER" "$gdm_tmp" <<'PY'
import sys
src, user, dst = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    text = open(src, encoding="utf-8").read()
except FileNotFoundError:
    text = "[daemon]\n"
lines = text.splitlines() if text.strip() else ["[daemon]"]
out, in_daemon, seen_enable, seen_user = [], False, False, False
for line in lines:
    stripped = line.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        if in_daemon:
            if not seen_enable:
                out.append("AutomaticLoginEnable=true")
            if not seen_user:
                out.append(f"AutomaticLogin={user}")
        in_daemon = (stripped.lower() == "[daemon]")
        out.append(line)
        continue
    if in_daemon and stripped.lower().startswith("automaticloginenable"):
        out.append("AutomaticLoginEnable=true"); seen_enable = True; continue
    if in_daemon and stripped.lower().startswith("automaticlogin="):
        out.append(f"AutomaticLogin={user}"); seen_user = True; continue
    out.append(line)
if in_daemon:
    if not seen_enable: out.append("AutomaticLoginEnable=true")
    if not seen_user: out.append(f"AutomaticLogin={user}")
elif not any(l.strip().lower() == "[daemon]" for l in out):
    out = ["[daemon]", "AutomaticLoginEnable=true", f"AutomaticLogin={user}", ""] + out
open(dst, "w", encoding="utf-8").write("\n".join(out) + "\n")
PY
    _sudo install -m 644 "$gdm_tmp" "$conf"
    rm -f "$gdm_tmp"
    echo "GDM auto-login enabled for $TARGET_USER ($conf)"
  elif [[ -d /etc/lightdm ]]; then
    conf=/etc/lightdm/lightdm.conf.d/50-aiforge-autologin.conf
    _sudo mkdir -p /etc/lightdm/lightdm.conf.d
    printf '[Seat:*]\nautologin-user=%s\nautologin-user-timeout=0\n' "$TARGET_USER" \
      | _sudo tee "$conf" >/dev/null
    echo "LightDM auto-login enabled for $TARGET_USER ($conf)"
  elif [[ -d /etc/sddm.conf.d ]] || command -v sddm >/dev/null 2>&1; then
    conf=/etc/sddm.conf.d/aiforge-autologin.conf
    _sudo mkdir -p /etc/sddm.conf.d
    printf '[Autologin]\nUser=%s\nSession=ubuntu\n' "$TARGET_USER" \
      | _sudo tee "$conf" >/dev/null
    echo "SDDM auto-login enabled for $TARGET_USER ($conf)"
  else
    echo "note: no GDM/LightDM/SDDM found — skipping greeter auto-login"
    echo "      (system aiforge-api.service still boots without a desktop login)"
  fi
else
  echo "note: AIFORGE_AUTO_LOGIN=0 — skipped display-manager auto-login"
fi

# ── 6. start now ──────────────────────────────────────────────────────────
_sudo systemctl restart aiforge-api.service || _sudo systemctl start aiforge-api.service
sleep 2
_sudo systemctl --no-pager --full status aiforge-api.service | head -20 || true

echo
echo "Done. After reboot:"
echo "  - multi-user.target starts aiforge-api (no login required)"
echo "  - docker + wg-quick@wg0 come up"
if [[ "$AUTO_LOGIN" == "1" ]]; then
  echo "  - display manager auto-logs in $TARGET_USER (no password at greeter)"
fi
echo "  - sudo -n works for boot commands (see $SUDOERS)"
echo
echo "Probe: curl -fsS http://127.0.0.1:8799/api/health"
