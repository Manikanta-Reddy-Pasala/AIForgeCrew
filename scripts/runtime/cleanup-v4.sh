#!/usr/bin/env bash
# v4 → v5 cleanup. Runs ON Mac Studio. Destructive — backs up first.
#
# Deletes: Paperclip (app, Postgres :54329, data dir), hermes agent, ChromaDB,
# hindsight daemon + its Postgres :5433. Preserves: aiforge Postgres :5432,
# LM Studio, bge-m3/reranker sidecars, graphify.
#
# Usage:
#   DRY_RUN=1 bash scripts/runtime/cleanup-v4.sh   # show what would happen
#   bash scripts/runtime/cleanup-v4.sh
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-$HOME/.aiforge/backups/$(date +%F)}"
DRY_RUN="${DRY_RUN:-0}"
PSQL=/Users/manikanta/.pg0/installation/18.1.0/bin/psql

run() {
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "DRY: $*"
  else
    eval "$@"
  fi
}

mkdir -p "$BACKUP_DIR"
echo ">>> backups → $BACKUP_DIR"

echo ">>> 1/6 dump Paperclip DB"
if "$PSQL" -h 127.0.0.1 -p 54329 -U paperclip -d paperclip -c '\q' 2>/dev/null; then
  run "PGPASSWORD=paperclip pg_dump -h 127.0.0.1 -p 54329 -U paperclip paperclip > $BACKUP_DIR/paperclip.sql"
fi

echo ">>> 2/6 dump hindsight DB"
if "$PSQL" -h 127.0.0.1 -p 5433 -U hindsight -d hindsight -c '\q' 2>/dev/null; then
  run "PGPASSWORD=hindsight pg_dump -h 127.0.0.1 -p 5433 -U hindsight hindsight > $BACKUP_DIR/hindsight.sql"
fi

echo ">>> 3/6 tar ~/.hermes"
if [[ -d "$HOME/.hermes" ]]; then
  run "tar -czf $BACKUP_DIR/hermes.tar.gz -C $HOME .hermes"
fi

echo ">>> 4/6 tar ChromaDB RAG"
if [[ -d "$HOME/AIForgeCrew/.aiforge/rag" ]]; then
  run "tar -czf $BACKUP_DIR/aiforge-rag.tar.gz -C $HOME/AIForgeCrew .aiforge/rag"
fi

echo ">>> 5/6 stop services (launchd + processes)"
for legacy in com.aiforge.paperclip com.aiforge.paperclip-tunnel com.aiforge.hermes-dashboard; do
  run "launchctl bootout gui/\$(id -u)/${legacy} 2>/dev/null || true"
  run "rm -f $HOME/Library/LaunchAgents/${legacy}.plist"
done
run "pkill -9 -f 'hindsight-embed|paperclipai|hermes chat|hermes dashboard' 2>/dev/null || true"
run "sleep 2"

echo ">>> 6/6 remove data dirs (kept in backups)"
run "rm -rf $HOME/.hermes"
run "rm -rf $HOME/.paperclip"
run "rm -rf $HOME/.hindsight $HOME/.pg0/instances/hindsight-embed-hermes"
run "rm -rf $HOME/AIForgeCrew/.aiforge/rag"

echo
echo "Done. Backups in $BACKUP_DIR"
echo "Verify v5 is live: launchctl list | grep aiforge.tick"
