#!/usr/bin/env bash
# File one ticket per repo under ~/codeRepo asking Planner+Doer to write
# or refresh README.md covering: 2-line overview, public APIs, features,
# memory references. Each Doer run also calls retain_fact to update T3
# skills/<repo> memory so future tickets retrieve it fast.
#
# Usage (Mac Studio):
#   bash scripts/file-repo-readme-tickets.sh            # all repos
#   REPO_LIST="PosClientBackend,BusinessService" bash scripts/file-repo-readme-tickets.sh
#   DRY_RUN=1 bash scripts/file-repo-readme-tickets.sh  # print would-do
set -euo pipefail

REPO="${REPO_ROOT:-$HOME/AIForgeCrew}"
CODEREPO="${CODEREPO:-$HOME/codeRepo}"
PY="$REPO/.venv/bin/python"
DRY="${DRY_RUN:-0}"

# Repos worth documenting (active code, not deployment-only manifests).
# Skip claude-memory, .kubeconfigs, AIForgeCrew itself.
EXCLUDE="AIForgeCrew|claude-memory|\.kubeconfigs|memory|skills|docs|CLAUDE\.md"

if [[ -n "${REPO_LIST:-}" ]]; then
  REPOS=(${REPO_LIST//,/ })
else
  REPOS=()
  while IFS= read -r d; do
    name=$(basename "$d")
    [[ -d "$d/.git" ]] || continue
    [[ "$name" =~ $EXCLUDE ]] && continue
    REPOS+=("$name")
  done < <(find "$CODEREPO" -maxdepth 1 -mindepth 1 -type d | sort)
fi

echo ">>> will file tickets for ${#REPOS[@]} repos"
for r in "${REPOS[@]}"; do printf "  - %s\n" "$r"; done
echo

for repo in "${REPOS[@]}"; do
  body="Write or refresh README.md for the \`$repo\` repo at \`~/codeRepo/$repo\`.

Required sections in README.md:
1. **Overview** — 2 lines describing what this service/library does.
2. **Public APIs / Endpoints / Entry points** — REST routes, GraphQL schemas, or Java/Python public classes + method signatures. Include brief one-liner per item.
3. **Features** — bulleted list of capabilities (5-10 bullets max).
4. **Memory references** — call \`search\` first for prior canon about this repo; cite any hits. Call \`read_claude_memory\` for operator notes that mention this repo.

Acceptance:
- README.md written/updated (single file in the repo root).
- \`mvn -q compile\` or \`python -m py_compile\` or \`npm run build\` still green (no code change expected, but verify the repo still compiles).
- One commit on the ticket branch.
- Doer calls \`retain_fact(tier='t3', wing='skills/$repo', text=<2-line overview anchored to top-level files>)\` after commit.

Scope strictly README.md; don't touch source."

  title="Write README.md for $repo (overview + APIs + features)"

  if [[ "$DRY" == "1" ]]; then
    printf "DRY: %s\n" "$title"
    continue
  fi
  cd "$REPO"
  "$PY" -m aiforge_core.runtime.cli create \
    --title "$title" --body "$body" --priority low 2>&1 | tail -1
  sleep 0.2
done
echo "done — watch: tail -f ~/.aiforge/logs/orchestrator-supervisor.ndjson | jq"
