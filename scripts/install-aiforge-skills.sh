#!/usr/bin/env bash
# scripts/install-aiforge-skills.sh — install the aiforge skill pack into
# ~/.hermes/skills/aiforge/ on the Mac Studio.
#
# Resolves the {{AIFORGE_BIN}} and {{AIFORGE_PY}} placeholders in each
# SKILL.md to the actual repo-local paths.
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "install-aiforge-skills: macOS only" >&2; exit 1
fi

REPO_DIR="${REPO_DIR:-$HOME/AIForgeCrew}"
HERMES_SKILLS="${HERMES_SKILLS:-$HOME/.hermes/skills}"
SKILL_SRC="$REPO_DIR/aiforge_core/skills"
DEST="$HERMES_SKILLS/aiforge"

[[ -d "$SKILL_SRC" ]] || { echo "skill sources missing at $SKILL_SRC — git pull first?" >&2; exit 1; }
[[ -x "$REPO_DIR/.venv/bin/aiforge" ]] || { echo "aiforge CLI missing — run scripts/install-aiforge.sh" >&2; exit 1; }

AIFORGE_BIN="$REPO_DIR/.venv/bin/aiforge"
AIFORGE_PY="$REPO_DIR/.venv/bin/python"

echo ">>> installing skill pack to $DEST"
rm -rf "$DEST"
mkdir -p "$DEST"

# DESCRIPTION.md for the pack.
cat > "$DEST/DESCRIPTION.md" <<EOF
# aiforge

AIForgeCrew policy + DESIGN-specific tooling exposed as Hermes skills.
Source: $REPO_DIR
Skills: lifecycle · coverage · rag · crg · memory · git · fetch · report

Backed by $AIFORGE_BIN + $AIFORGE_PY. Repo-relative paths assume the Hermes
session cwd is the AIForgeCrew working tree (or set with --working-dir).
EOF

# Copy + expand placeholders.
for skill_dir in "$SKILL_SRC"/*/; do
  [[ -d "$skill_dir" ]] || continue
  name=$(basename "$skill_dir")
  mkdir -p "$DEST/$name"
  for f in "$skill_dir"*; do
    base=$(basename "$f")
    sed "s|{{AIFORGE_BIN}}|$AIFORGE_BIN|g; s|{{AIFORGE_PY}}|$AIFORGE_PY|g" "$f" > "$DEST/$name/$base"
  done
  echo "  installed: aiforge/$name"
done

echo
echo "Installed. Verify with:"
echo "  ls $DEST"
