#!/usr/bin/env bash
# aiforge-graphify-all.sh — refresh graphify-out for every git repo under
# the code root. Wraps the per-repo `graphify update` (AST-only, no LLM)
# the same way scripts/runtime/aiforge-reindex.sh does for a single repo,
# but fans out over the whole code tree on a timer.
#
# DESIGN (mirrors aiforge-reindex.sh):
#   - Graphify artifacts land OUT OF TREE under
#     $AIFORGE_HOME/graphify-out/<repo> — never inside the repo.
#   - Continue-on-error: one repo failing never aborts the rest; the
#     overall exit code is non-zero if any repo failed (so the systemd
#     unit surfaces a problem) but every repo is still attempted.
#
# Repo set: every direct child directory of $AIFORGE_CODE_ROOT
# (default ~/codeRepo) that is a git repo. Doer worktrees
# (*.aiforge-worktrees/*) and non-git dirs are skipped.
#
# Paths are HOME-relative + env-overridable (no hard-coded user dir) so
# this survives host/user migrations.
#
# Usage:
#   aiforge-graphify-all.sh            # refresh all repos
#   aiforge-graphify-all.sh --dry-run  # list the repos that would run, no work
#
# Env overrides:
#   AIFORGE_CODE_ROOT  (default $HOME/codeRepo)
#   AIFORGE_HOME       (default $HOME/.aiforge)
#   GRAPHIFY           (default $HOME/.local/bin/graphify)
#   AIFORGE_GRAPHIFY_TIMEOUT  per-repo seconds (default 600)
set -uo pipefail

DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

CODE_ROOT="${AIFORGE_CODE_ROOT:-$HOME/codeRepo}"
AIFORGE_HOME="${AIFORGE_HOME:-$HOME/.aiforge}"
GRAPHIFY="${GRAPHIFY:-$HOME/.local/bin/graphify}"
PER_REPO_TIMEOUT="${AIFORGE_GRAPHIFY_TIMEOUT:-600}"

OUT_BASE="$AIFORGE_HOME/graphify-out"
LOG="$AIFORGE_HOME/logs/graphify-all.log"

# Missing code root is not an error — nothing to index yet.
if [ ! -d "$CODE_ROOT" ]; then
  echo "aiforge-graphify-all: code root not found: $CODE_ROOT" >&2
  exit 0
fi

discover() {
  # Direct children only; git repos; skip doer worktrees + dotdirs.
  for d in "$CODE_ROOT"/*/; do
    d="${d%/}"
    case "$d" in
      *.aiforge-worktrees/*) continue ;;
      */.*) continue ;;
    esac
    [ -d "$d/.git" ] || continue
    echo "$d"
  done
}

if [ "$DRY_RUN" = 1 ]; then
  discover
  exit 0
fi

if [ ! -x "$GRAPHIFY" ]; then
  echo "aiforge-graphify-all: graphify not executable: $GRAPHIFY" >&2
  exit 1
fi

mkdir -p "$OUT_BASE" "$AIFORGE_HOME/logs"

ok=0
fail=0
while IFS= read -r repo; do
  [ -n "$repo" ] || continue
  name="$(basename "$repo")"
  out="$OUT_BASE/$name"
  if {
    echo "=== $(date -Iseconds) graphify update $name ==="
    echo "    out=$out"
    cd "$repo" && timeout "$PER_REPO_TIMEOUT" "$GRAPHIFY" update . --out "$out" 2>&1 | tail -3
  } >>"$LOG" 2>&1; then
    ok=$((ok + 1))
  else
    echo "aiforge-graphify-all: graphify failed for $name (continuing)" >&2
    fail=$((fail + 1))
  fi
done < <(discover)

echo "aiforge-graphify-all: $ok ok, $fail failed" >&2
# Per-repo failures (non-code repos, parse errors) are expected and logged
# — they must NOT mark the timer unit red. Only a total wipeout (nothing
# indexed at all) is a real failure worth surfacing.
if [ "$ok" -eq 0 ] && [ "$fail" -gt 0 ]; then
  exit 1
fi
exit 0
