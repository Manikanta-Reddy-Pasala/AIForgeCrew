#!/usr/bin/env bash
# Keep this machine awake until you stop the script (Ctrl-C).
#
# Screen LOCK is never the problem — every platform keeps running through it.
# SLEEP is: it suspends the whole process, so a long AIForge run comes back to
# a dead model socket with all its finished work waiting to be re-done.
#
# Run it in a terminal next to the work, and stop it when you are done:
#
#     ./scripts/keep-awake.sh
#     ./scripts/keep-awake.sh -- ./run.sh          # or wrap a command
#
# With a command after `--`, the assertion is held for exactly as long as that
# command runs and released when it exits, however it exits.
#
# NOTHING here is permanent. No power setting is changed, so nothing is left
# behind if this script is killed -9 — which is the whole reason it is a
# foreground process holding an assertion rather than `powercfg /change`.
# The screen is still free to lock: keeping a machine awake is reasonable,
# keeping it unlocked is not.
#
# AIForge already does this for itself around team runs and scheduled jobs
# (aiforge_core/runtime/keep_awake.py). This script is for the other case:
# your own long command, a manual pull-and-rebuild, a big test run.
set -euo pipefail

CMD=()
if [[ "${1:-}" == "--" ]]; then
  shift
  CMD=("$@")
fi

is_wsl() { grep -qi microsoft /proc/version 2>/dev/null; return; }

start_holder() {
  case "$(uname -s)" in
    Darwin)
      command -v caffeinate >/dev/null || return 1
      # -d display, -i idle, -m disk, -s system-while-on-AC. A closed laptop on
      # battery is still allowed to sleep — a run must not flatten it.
      caffeinate -dims &
      ;;
    Linux)
      if is_wsl; then
        # Windows owns the power policy even though the shell is in the distro.
        # ES_CONTINUOUS | ES_SYSTEM_REQUIRED = 0x80000001. Deliberately NOT
        # ES_DISPLAY_REQUIRED: the screen must still lock. Runs as the ordinary
        # Windows user — no elevation, no UAC prompt.
        local ps
        ps="$(command -v powershell.exe || true)"
        [[ -n "$ps" ]] || return 1
        "$ps" -NoProfile -NonInteractive -Command \
          '$s = Add-Type -MemberDefinition "[DllImport(\"kernel32.dll\", SetLastError=true)] public static extern uint SetThreadExecutionState(uint e);" -Name Pwr -Namespace AIForge -PassThru; $s::SetThreadExecutionState(0x80000001) | Out-Null; while ($true) { Start-Sleep -Seconds 60 }' &
      else
        command -v systemd-inhibit >/dev/null || return 1
        systemd-inhibit --what=sleep:idle --who=AIForge \
          --why="a run is in flight" --mode=block sleep infinity &
      fi
      ;;
    *)
      return 1
      ;;
  esac
  HOLDER=$!
  return 0
}

if ! start_holder; then
  echo "keep-awake: no power API on this box (need caffeinate, powershell.exe or systemd-inhibit)." >&2
  echo "keep-awake: running your command anyway — the machine may still sleep." >&2
  HOLDER=""
fi

cleanup() {
  if [[ -n "${HOLDER:-}" ]]; then
    kill "$HOLDER" 2>/dev/null || true
    wait "$HOLDER" 2>/dev/null || true
  fi
  return
}
trap cleanup EXIT INT TERM

if [[ -n "${HOLDER:-}" ]]; then
  echo "keep-awake: holding (pid $HOLDER). The screen still locks normally."
fi

if [[ ${#CMD[@]} -gt 0 ]]; then
  # Run the command in the foreground; the assertion dies with it via the trap.
  "${CMD[@]}"
  exit $?
fi

echo "keep-awake: press Ctrl-C to release."
# `wait` on the holder rather than a sleep loop: if the holder dies (a
# `powercfg` change, a killed process), this exits instead of pretending.
if [[ -n "${HOLDER:-}" ]]; then
  wait "$HOLDER" || true
  echo "keep-awake: the holder exited — the machine can sleep again." >&2
else
  while true; do sleep 3600; done
fi
