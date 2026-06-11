#!/usr/bin/env bash
# One-shot NUC deploy for the AIForge stack. Idempotent; run ON the NUC:
#
#   cd ~/AIForgeCrew && git pull --ff-only && bash scripts/runtime/nuc/deploy.sh
#
# Steps: pull both repos → reinstall packages → sync systemd user units
# (now %h-relative, no hardcoded home) → restart services → enable all
# timers (incl. aiforge-memory-decay) → health checks.
set -euo pipefail

CREW="${AIFORGE_CREW_DIR:-$HOME/AIForgeCrew}"
AFM="${AIFORGE_AFM_DIR:-$HOME/AiForgeMemory}"
UNIT_SRC="$CREW/scripts/runtime/nuc"
UNIT_DST="$HOME/.config/systemd/user"

step() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

step "pull repos"
git -C "$CREW" pull --ff-only
if [ -d "$AFM/.git" ]; then
    git -C "$AFM" pull --ff-only
else
    echo "WARN: $AFM missing — clone AiForgeMemory first" >&2
fi

step "install packages"
"$CREW/.venv/bin/pip" install -q -e "$CREW"
if [ -d "$AFM" ]; then
    # AFM installs into the same venv the runner/api import from.
    "$CREW/.venv/bin/pip" install -q -e "$AFM"
fi

step "sync systemd user units"
mkdir -p "$UNIT_DST"
cp "$UNIT_SRC"/*.service "$UNIT_SRC"/*.timer "$UNIT_DST"/
systemctl --user daemon-reload

step "restart services"
systemctl --user restart aiforge-api
for svc in aiforge-embed-sidecar aiforge-rerank-sidecar; do
    systemctl --user restart "$svc" 2>/dev/null \
        || echo "note: $svc not active (ok if model files absent)"
done
# The graph runner unit lives only on some hosts; restart when present.
for runner in aiforge-runner aiforge-graph-runner graph-runner; do
    if systemctl --user list-unit-files "$runner.service" --no-legend \
            2>/dev/null | grep -q "$runner"; then
        systemctl --user restart "$runner" && echo "restarted $runner"
    fi
done

step "enable timers"
for t in aiforge-file-indexer.timer aiforge-reindex-daily.timer \
         aiforge-git-pull.timer aiforge-repo-pull.timer \
         aiforge-memory-decay.timer aiforge-pr-comments.timer \
         aiforge-pattern-miner.timer aiforge-symbol-embed.timer \
         aiforge-worktree-janitor.timer; do
    if [ -f "$UNIT_DST/$t" ]; then
        systemctl --user enable --now "$t" 2>/dev/null \
            && echo "enabled $t" || echo "WARN: enable failed: $t"
    fi
done

step "health checks"
fail=0
check() {  # check <name> <cmd...>
    local name="$1"; shift
    if "$@" >/dev/null 2>&1; then echo "OK   $name"
    else echo "FAIL $name"; fail=1; fi
}
check "api :8799/health"       curl -fsS -m 5 http://127.0.0.1:8799/health
check "embed sidecar :8764"    curl -fsS -m 5 http://127.0.0.1:8764/health
check "rerank sidecar :8765"   curl -fsS -m 5 http://127.0.0.1:8765/health
check "neo4j bolt :7687"       bash -c 'exec 3<>/dev/tcp/127.0.0.1/7687'
check "postgres :5432"         bash -c 'exec 3<>/dev/tcp/127.0.0.1/5432'
check "decay timer enabled"    systemctl --user is-enabled aiforge-memory-decay.timer

step "AiForgeMemory scheduler registration"
if [ -f "$HOME/.aiforge/scheduler.yaml" ]; then
    echo "registered repos:"
    grep -E "^\s*-?\s*name:|^\s*repos:" "$HOME/.aiforge/scheduler.yaml" || true
else
    echo "WARN: ~/.aiforge/scheduler.yaml missing — periodic code ingest is OFF"
fi

if [ "$fail" -ne 0 ]; then
    echo; echo "DEPLOY COMPLETED WITH FAILED HEALTH CHECKS — inspect above." >&2
    exit 1
fi
echo; echo "Deploy complete. Next: run one live ticket end-to-end and watch"
echo "journalctl --user -u aiforge-api -f for the Workflow stage events."
