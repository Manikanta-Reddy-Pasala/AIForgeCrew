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

Required sections in README.md (write as much as needed per section — not a character budget, write until it's genuinely useful):

1. **Overview** — paragraph(s) describing what this service/library does, who it talks to, its role in the OneShell system. Include short architecture context: upstream/downstream dependencies, sync/async flow, deployment surface (Docker / Kubernetes \`pos\` namespace / client-only / etc.).

2. **Public APIs / Endpoints / Entry points** — full catalog with brief description of each:
   - REST routes: method + path + request/response shape
   - GraphQL schemas: query/mutation names + args
   - Java classes: public class + key public method signatures + what they do
   - Python/TS modules: main functions + purpose
   - Event consumers / publishers: NATS subjects, MongoDB change streams, Kafka topics
   - Scheduled jobs / cron entries

3. **Features** — bulleted list. Don't cap the count. Each bullet 1-2 sentences.

4. **Design patterns observed** — concrete names + where used. Name the pattern + cite at least one file:line anchor.

5. **Coding style + conventions** — what's idiomatic in this repo. Logging convention (loguru vs slf4j), null-handling (Optional<>, Mono.switchIfEmpty), exception style, test harness pattern. Cite a representative file:line per convention.

6. **Improvements / known issues** — things that would benefit from refactoring. Duplicated code, missing tests, stale annotations, performance hotspots visible from code. Don't invent — only what's observable in the source. Tag \`(speculative)\` if uncertain.

7. **Memory references** — call \`search\` first for prior canon about this repo; cite any hits. Call \`read_operator_memory(query=\"$repo\")\` for operator notes.

Acceptance:
- README.md written/updated in the repo root.
- \`mvn -q compile\` / \`python -m py_compile\` / \`npm run build\` still green (no code change expected; verify the repo still compiles).
- One commit on the ticket branch.
- Doer calls \`retain_fact(tier='t3', wing='skills/$repo', text=<one dense line summarizing the repo + design patterns, anchored to top-level files>)\` after commit.
- Feedback: pass the README if it covers all 7 sections with file:line anchors and no speculative claims remain uncited.

Scope strictly README.md (plus reading the repo's code for reference). Do NOT modify source."

  title="Write README.md for $repo (overview + APIs + features)"

  if [[ "$DRY" == "1" ]]; then
    printf "DRY: %s\n" "$title"
    continue
  fi
  cd "$REPO"
  "$PY" -m aiforge_core.cli.main create \
    --title "$title" --body "$body" --priority low 2>&1 | tail -1
  sleep 0.2
done
echo "done — watch: tail -f ~/.aiforge/logs/orchestrator-supervisor.ndjson | jq"
