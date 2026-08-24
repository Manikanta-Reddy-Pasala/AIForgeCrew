#!/bin/bash
# Janitor: remove .aiforge-worktrees/ONE-* whose ticket is in a terminal status.
# No default password. Shipping one in git means the fallback IS the
# credential everywhere the variable is unset, which is everywhere nobody
# thought to set it.
PGPASS="${AIFORGE_PG_PASSWORD:?set AIFORGE_PG_PASSWORD (no default is shipped)}"
ROOT="${AIFORGE_WORKTREE_ROOT:-$HOME/codeRepo}"
removed=0
for wt in "$ROOT"/*/.aiforge-worktrees/*; do
  [ -d "$wt" ] || continue
  ticket=$(basename "$wt")
  case "$ticket" in
    ONE-*) ;;
    *) continue ;;
  esac
  repo=$(dirname "$wt" | xargs dirname | xargs basename)
  status=$(PGPASSWORD=$PGPASS psql -h 127.0.0.1 -U aiforge aiforge -t -A -c "SELECT status FROM tickets WHERE identifier='$ticket'" 2>/dev/null)
  case "$status" in
    done|blocked|failed|cancelled)
      echo "[$repo/$ticket] status=$status -> remove"
      cd "$ROOT/$repo" 2>/dev/null || continue
      git worktree remove --force "$wt" 2>&1 | tail -1
      removed=$((removed+1))
      ;;
    in_progress|todo|"")
      echo "[$repo/$ticket] status=$status -> keep (active)"
      ;;
    *)
      echo "[$repo/$ticket] status=$status -> keep (unknown)"
      ;;
  esac
done
# Prune any tracked-but-missing worktree refs
for repo in "$ROOT"/*/.git; do
  cd "$(dirname "$repo")" || continue
  git worktree prune 2>&1 | tail -1
done
echo "removed=$removed"
