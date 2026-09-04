#!/usr/bin/env bash
# Snapshot the current agent_config.json + per-role temp/think env vars,
# then apply a new profile in one shot. Restarts aiforge-api and
# aiforge-graph-runner so the new config is picked up immediately.
#
# Usage:
#   aiforge-model-swap.sh save                    # snapshot only
#   aiforge-model-swap.sh apply <profile-name>    # snapshot + apply
#   aiforge-model-swap.sh restore <snapshot-id>   # roll back to a snapshot
#   aiforge-model-swap.sh list                    # list snapshots + profiles
#
# Profiles live at ~/.aiforge/model-profiles/<name>.json. They specify
# per-role model paths, base_urls, and the env vars to write into
# ~/.aiforge/runtime.env.
set -eu

AIFORGE_HOME="${AIFORGE_HOME:-$HOME/.aiforge}"
SNAP_DIR="$AIFORGE_HOME/model-snapshots"
PROFILE_DIR="$AIFORGE_HOME/model-profiles"
CFG="$AIFORGE_HOME/agent_config.json"
ENV_FILE="$AIFORGE_HOME/runtime.env"
mkdir -p "$SNAP_DIR" "$PROFILE_DIR"

snapshot_now() {
  local id="snap-$(date +%Y%m%d-%H%M%S)"
  local out="$SNAP_DIR/$id.json"
  python3 - "$CFG" "$ENV_FILE" "$out" <<'PY'
import json, os, sys, datetime
cfg_path, env_path, out_path = sys.argv[1:4]
data = {
    "captured_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "agent_config": json.load(open(cfg_path)) if os.path.isfile(cfg_path) else {},
    "runtime_env": {},
}
if os.path.isfile(env_path):
    for line in open(env_path):
        line = line.strip()
        if not line or line.startswith("#"): continue
        if "=" not in line: continue
        k, v = line.split("=", 1)
        if k.startswith("AIFORGE_"):
            data["runtime_env"][k] = v
json.dump(data, open(out_path, "w"), indent=2)
print(out_path)
PY
  return
}

case "${1:-}" in
  save)
    out=$(snapshot_now)
    echo "snapshot saved: $out"
    ;;
  list)
    echo "=== snapshots ==="
    ls -1tr "$SNAP_DIR" 2>/dev/null || echo "(none)"
    echo "=== profiles ==="
    ls -1tr "$PROFILE_DIR" 2>/dev/null || echo "(none)"
    ;;
  apply)
    name="${2:-}"
    [[ -z "$name" ]] && { echo "usage: apply <profile>"; exit 2; }
    profile="$PROFILE_DIR/$name.json"
    [[ ! -f "$profile" ]] && { echo "profile not found: $profile"; exit 2; }
    snap=$(snapshot_now)
    echo "snapshot saved: $snap"
    python3 - "$CFG" "$ENV_FILE" "$profile" <<'PY'
import json, os, sys
cfg_path, env_path, profile_path = sys.argv[1:4]
prof = json.load(open(profile_path))

# 1. Merge agent_config.json — only roles listed in the profile are
# overwritten. Untouched roles keep their existing settings.
cfg = json.load(open(cfg_path)) if os.path.isfile(cfg_path) else {}
for role, role_cfg in prof.get("agent_config", {}).items():
    cfg.setdefault(role, {}).update(role_cfg)
json.dump(cfg, open(cfg_path, "w"), indent=2)

# 2. Update runtime.env. Profile env keys are upserted; everything else
# (DSN, paths, etc.) is preserved.
existing: dict[str, str] = {}
if os.path.isfile(env_path):
    for line in open(env_path):
        s = line.rstrip("\n")
        if not s.strip() or s.lstrip().startswith("#"):
            continue
        if "=" in s:
            k, v = s.split("=", 1)
            existing[k] = v

for k, v in prof.get("runtime_env", {}).items():
    existing[k] = str(v)

# Drop keys explicitly listed under runtime_env_remove.
for k in prof.get("runtime_env_remove", []):
    existing.pop(k, None)

with open(env_path, "w") as f:
    for k, v in existing.items():
        f.write(f"{k}={v}\n")
print(f"applied profile: {profile_path}")
PY
    echo "restarting aiforge-api + aiforge-graph-runner..."
    systemctl --user restart aiforge-api
    systemctl --user restart aiforge-graph-runner
    ;;
  restore)
    sid="${2:-}"
    [[ -z "$sid" ]] && { echo "usage: restore <snapshot-id>"; exit 2; }
    snap_path="$SNAP_DIR/$sid.json"
    [[ ! -f "$snap_path" ]] && snap_path="$SNAP_DIR/$sid"
    [[ ! -f "$snap_path" ]] && { echo "snapshot not found: $sid"; exit 2; }
    python3 - "$CFG" "$ENV_FILE" "$snap_path" <<'PY'
import json, sys
cfg_path, env_path, snap_path = sys.argv[1:4]
snap = json.load(open(snap_path))
json.dump(snap.get("agent_config", {}), open(cfg_path, "w"), indent=2)
existing: dict[str, str] = {}
import os
if os.path.isfile(env_path):
    for line in open(env_path):
        s = line.rstrip("\n")
        if not s.strip() or s.lstrip().startswith("#"):
            continue
        if "=" in s:
            k, v = s.split("=", 1)
            existing[k] = v
# Strip all AIFORGE_* keys then re-apply snapshot ones (clean swap).
for k in list(existing):
    if k.startswith("AIFORGE_"):
        existing.pop(k, None)
for k, v in snap.get("runtime_env", {}).items():
    existing[k] = v
with open(env_path, "w") as f:
    for k, v in existing.items():
        f.write(f"{k}={v}\n")
print(f"restored from {snap_path}")
PY
    systemctl --user restart aiforge-api
    systemctl --user restart aiforge-graph-runner
    ;;
  *)
    echo "usage: $0 save|apply <profile>|restore <snap-id>|list"
    exit 2
    ;;
esac
