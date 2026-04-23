#!/usr/bin/env bash
# Pull every git repo under ~/codeRepo. Called by aiforge-repo-pull.timer
# (every 5 min). Replaces the old Mac Studio → NUC rsync mirror.
set -u
ROOT="${AIFORGE_CODE_ROOT:-$HOME/codeRepo}"
[ -d "$ROOT" ] || { echo "missing $ROOT"; exit 1; }

n_ok=0 n_fail=0 n_skip=0

# Pull ~/.claude/memory too (shared memory bank)
if [ -d "$HOME/.claude/memory/.git" ]; then
    if git -C "$HOME/.claude/memory" pull --ff-only --quiet 2>/dev/null; then
        n_ok=$((n_ok + 1))
    else
        n_fail=$((n_fail + 1))
        echo "FAIL .claude/memory"
    fi
fi

for d in "$ROOT"/*/; do
    if [ ! -d "$d/.git" ]; then
        n_skip=$((n_skip + 1))
        continue
    fi
    if git -C "$d" pull --ff-only --quiet 2>/dev/null; then
        n_ok=$((n_ok + 1))
    else
        n_fail=$((n_fail + 1))
        echo "FAIL $(basename "$d")"
    fi
done
echo "pulled=$n_ok fail=$n_fail skip=$n_skip"
