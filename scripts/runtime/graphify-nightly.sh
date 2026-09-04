#!/usr/bin/env bash
# Nightly Graphify rebuild + Neo4j mirror.
#
# For every repo in $REPOS_DIR/$REPO_LIST that exists, runs
# `graphify update <repo>` to refresh that repo's graphify-out/graph.json.
# Then merges all per-repo graphs into one combined graph.json (Python),
# then loads the merged graph into Neo4j via
# `python -m aiforge_core.indexing.graphify_loader`.
#
# Designed for launchd / cron / systemd. Per-repo failures are logged and
# do not abort the whole run.
#
# Env knobs (all optional):
#   REPOS_DIR    parent dir holding all repos     default: /Users/manikanta/codeRepo
#   OUT_DIR      where to mirror per-repo graphs   default: /Users/manikanta/.aiforge/graphify-out
#   REPO_LIST    space-separated repo names,
#                or path to file with one per line
#                default: built-in AIForgeCrew set
#   GRAPHIFY_BIN absolute path to graphify CLI    default: /Users/manikanta/.local/bin/graphify
#   AIFORGE_VENV venv with neo4j driver           default: /Users/manikanta/AIForgeCrew/.venv
#   AIFORGE_REPO repo root used for PYTHONPATH    default: /Users/manikanta/AIForgeCrew
#   AIFORGE_NEO4J_URI / USER / PASSWORD          inherited by loader

set -uo pipefail

REPOS_DIR="${REPOS_DIR:-/Users/manikanta/codeRepo}"
OUT_DIR="${OUT_DIR:-/Users/manikanta/.aiforge/graphify-out}"
GRAPHIFY_BIN="${GRAPHIFY_BIN:-/Users/manikanta/.local/bin/graphify}"
AIFORGE_VENV="${AIFORGE_VENV:-/Users/manikanta/AIForgeCrew/.venv}"
AIFORGE_REPO="${AIFORGE_REPO:-/Users/manikanta/AIForgeCrew}"
LOG_DIR="${LOG_DIR:-/Users/manikanta/.aiforge/logs}"

DEFAULT_REPOS=(
  PosClientBackend
  PosServerBackend
  MongoDbService
  BusinessService
  GatewayService
  Scheduler
  QuartzScheduler
  EmailService
  PosPythonBackend
  oneshell-commons
  PosService
  WhatsappApiService
  GstApiService
  NotificationService
  CacheLayer
  PosFrontend
  PosAdmin
  AIForgeCrew
)

# Resolve repo list.
if [[ -n "${REPO_LIST:-}" ]]; then
  if [[ -f "$REPO_LIST" ]]; then
    mapfile -t REPOS < <(grep -v '^\s*#' "$REPO_LIST" | awk 'NF')
  else
    # space-separated string
    read -r -a REPOS <<< "$REPO_LIST"
  fi
else
  REPOS=("${DEFAULT_REPOS[@]}")
fi

mkdir -p "$OUT_DIR" "$LOG_DIR"

ts() { date '+%Y-%m-%dT%H:%M:%S%z'; return; }
log() { echo "[$(ts)] $*"; return; }

if [[ ! -x "$GRAPHIFY_BIN" ]]; then
  log "ERROR: graphify CLI not found or not executable at $GRAPHIFY_BIN"
  exit 2
fi

GRAPHIFY_VERSION="$("$GRAPHIFY_BIN" --version 2>&1 || echo unknown)"
log "graphify-nightly start :: $GRAPHIFY_VERSION"
log "REPOS_DIR=$REPOS_DIR OUT_DIR=$OUT_DIR repos=${#REPOS[@]}"

OK_REPOS=()
FAIL_REPOS=()

for repo in "${REPOS[@]}"; do
  repo_path="$REPOS_DIR/$repo"
  if [[ ! -d "$repo_path" ]]; then
    log "SKIP $repo (not found at $repo_path)"
    continue
  fi
  log "GRAPHIFY $repo"
  # graphify v0.4.x writes graphify-out/ inside the repo dir; we run it
  # from inside the repo and then copy/symlink the output to a stable
  # central location so the loader has one place to look.
  if (cd "$repo_path" && "$GRAPHIFY_BIN" update . </dev/null) >>"$LOG_DIR/graphify-nightly.out" 2>>"$LOG_DIR/graphify-nightly.err"; then
    src_json="$repo_path/graphify-out/graph.json"
    if [[ -s "$src_json" ]]; then
      dest_dir="$OUT_DIR/$repo"
      mkdir -p "$dest_dir"
      # Copy (not symlink) so a later `git clean -fdx` in the repo won't
      # blow away our nightly snapshot.
      cp "$src_json" "$dest_dir/graph.json"
      OK_REPOS+=("$repo")
      log "  -> $(wc -c <"$src_json" | awk '{print $1}') bytes copied to $dest_dir/graph.json"
    else
      log "WARN $repo : graphify finished but produced no graph.json"
      FAIL_REPOS+=("$repo")
    fi
  else
    log "WARN $repo : graphify update failed (see graphify-nightly.err)"
    FAIL_REPOS+=("$repo")
  fi
done

log "graphify pass complete: ok=${#OK_REPOS[@]} fail=${#FAIL_REPOS[@]}"

# Merge per-repo graph.json files into one combined graph.json. v0.4.23
# has no `merge-graphs` subcommand so we do it inline with the project
# venv's Python (which already has json + the rest of stdlib).
PY="$AIFORGE_VENV/bin/python"
[[ -x "$PY" ]] || PY="python3"

MERGED_JSON="$OUT_DIR/merged.json"
log "MERGE -> $MERGED_JSON"
"$PY" - <<PYEOF >>"$LOG_DIR/graphify-nightly.out" 2>>"$LOG_DIR/graphify-nightly.err"
import json, glob, os, sys
out_dir = os.environ.get("OUT_DIR", "$OUT_DIR")
merged = {
    "directed": False,
    "multigraph": False,
    "graph": {"merged": True},
    "nodes": [],
    "links": [],
}
seen_nodes = set()
seen_edges = set()
for path in sorted(glob.glob(os.path.join(out_dir, "*", "graph.json"))):
    repo = os.path.basename(os.path.dirname(path))
    try:
        g = json.load(open(path))
    except Exception as exc:
        print(f"SKIP {path}: {exc}", file=sys.stderr)
        continue
    for n in g.get("nodes", []) or []:
        nid = n.get("id")
        if not nid or nid in seen_nodes:
            continue
        seen_nodes.add(nid)
        n = dict(n)
        n.setdefault("repo", repo)
        merged["nodes"].append(n)
    for e in g.get("links", g.get("edges", [])) or []:
        key = (e.get("source"), e.get("target"), e.get("relation"))
        if not key[0] or not key[1] or key in seen_edges:
            continue
        seen_edges.add(key)
        e = dict(e)
        e.setdefault("repo", repo)
        merged["links"].append(e)
json.dump(merged, open(os.path.join(out_dir, "merged.json"), "w"))
print(f"MERGED nodes={len(merged['nodes'])} links={len(merged['links'])}")
PYEOF

if [[ ! -s "$MERGED_JSON" ]]; then
  log "ERROR merged.json missing or empty; abort load"
  exit 3
fi

log "LOAD merged graph -> Neo4j"
export PYTHONPATH="$AIFORGE_REPO${PYTHONPATH:+:$PYTHONPATH}"
if "$PY" -m aiforge_core.indexing.graphify_loader \
        --graph "$MERGED_JSON" \
        --repo "_merged" \
        >>"$LOG_DIR/graphify-nightly.out" 2>>"$LOG_DIR/graphify-nightly.err"; then
  log "LOAD ok"
else
  log "ERROR loader failed"
  exit 4
fi

log "graphify nightly complete: ok=${#OK_REPOS[@]} fail=${#FAIL_REPOS[@]}"
exit 0
