#!/usr/bin/env bash
# scripts/hermes-install-skills.sh — install all Hermes skills used by
# AIForgeCrew roles (EM, Tester, Sr Developer, Sr Architect) on Mac Studio.
#
# Installs:
#   - Official optional skills via `hermes skills install official/<path>`
#   - Community skills via git clone into ~/.hermes/skills/
#
# Bundled skills (software-development/*, github/*, autonomous-ai-agents/*,
# dogfood, codebase-inspection, etc.) ship with Hermes and need no install.
#
# Idempotent. Safe to re-run.
set -euo pipefail

[[ "$(uname -s)" == "Darwin" ]] || { echo "hermes-install-skills: macOS only" >&2; exit 1; }

HERMES_BIN="${HERMES_BIN:-$HOME/.local/bin/hermes}"
HERMES_SKILLS="${HERMES_SKILLS:-$HOME/.hermes/skills}"

[[ -x "$HERMES_BIN" ]] || { echo "hermes CLI missing at $HERMES_BIN — run scripts/install-hermes-agent.sh" >&2; exit 1; }

mkdir -p "$HERMES_SKILLS"

# ---- 1. Official optional skills ----
# Enumerated per docs/reference/optional-skills-catalog.
# Install via `hermes skills install official/<category>/<skill>`.
OFFICIAL_SKILLS=(
  # Core agent infra
  "official/autonomous-ai-agents/honcho"                     # cross-session user/human modeling for EM

  # DevOps
  "official/devops/docker-management"                        # POS Docker + K8s images

  # MLOps — model benchmarking + pulls for Sr Architect
  "official/mlops/evaluation/evaluating-llms-harness"
  "official/mlops/huggingface-hub"

  # MCP authoring (if we need custom bridges later)
  "official/mcp/fastmcp"

  # Research / context
  "official/research/arxiv"
  "official/research/qmd"                                    # BM25+vector+rerank local KB (backup retrieval)
  "official/research/duckduckgo-search"
)

echo ">>> installing ${#OFFICIAL_SKILLS[@]} official optional skills"
for skill in "${OFFICIAL_SKILLS[@]}"; do
  echo "  [install] $skill"
  "$HERMES_BIN" skills install "$skill" || echo "    (skill install reported non-zero; continuing)"
done

# ---- 2. Community skills (git clone) ----
# name|repo_url|subpath_in_repo(optional, "" if skills live at repo root)
COMMUNITY_SKILLS=(
  "hindsight|https://github.com/NousResearch/hermes-agent.git|plugins/memory/hindsight"
  "hermes-incident-commander|https://github.com/0xNyk/hermes-incident-commander.git|"
  "hermes-council|https://github.com/0xNyk/hermes-council.git|"
  "hermes-dojo|https://github.com/0xNyk/hermes-dojo.git|"
  "hermes-skill-factory|https://github.com/0xNyk/hermes-skill-factory.git|"
)

clone_skill() {
  local name="$1" url="$2" subpath="$3"
  local dest="$HERMES_SKILLS/$name"
  local tmp="/tmp/hermes-skill-${name}-$$"

  if [[ -d "$dest/.git" ]]; then
    echo "  [pull ] $name"
    (cd "$dest" && git pull --ff-only --quiet) || echo "    (pull failed — leaving as-is)"
    return 0
  fi

  echo "  [clone] $name <- $url"
  rm -rf "$tmp" "$dest"
  if ! git clone --depth 1 --quiet "$url" "$tmp" 2>/dev/null; then
    echo "    (clone failed — skill not installed; $url may be private/unavailable)"
    rm -rf "$tmp"
    return 0
  fi
  if [[ -n "$subpath" && -d "$tmp/$subpath" ]]; then
    mv "$tmp/$subpath" "$dest"
    rm -rf "$tmp"
  else
    mv "$tmp" "$dest"
  fi
}

echo
echo ">>> cloning ${#COMMUNITY_SKILLS[@]} community skills into $HERMES_SKILLS"
for entry in "${COMMUNITY_SKILLS[@]}"; do
  IFS='|' read -r name url subpath <<< "$entry"
  clone_skill "$name" "$url" "$subpath"
done

# ---- 3. Summary ----
echo
echo "=== installed skills ==="
"$HERMES_BIN" skills list 2>/dev/null | head -80 || ls "$HERMES_SKILLS"

echo
echo "Next:"
echo "  scripts/hermes-setup-hindsight.sh    # enable Hindsight memory provider"
echo "  scripts/hermes-seed-memory.sh        # seed Hindsight from Claude memory"
