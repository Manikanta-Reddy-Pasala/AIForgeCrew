#!/usr/bin/env bash
# Assign the lightweight judge/reviewer model to the single-turn judge
# archetypes, leaving the Doer-class roles on the primary coder model.
#
# Idempotent; run on the NUC after deploy. Reverse with REVERT=1.
#
#   JUDGE_MODEL  (default nex-n2-mini-nvfp4)
#   ROLES        (default "triage feedback verify_correctness verify_scope verify_risk")
#   REVERT=1     reassign the roles back to the dynamic local default
set -euo pipefail

JUDGE_MODEL="${JUDGE_MODEL:-nex-n2-mini-nvfp4}"
ROLES="${ROLES:-triage feedback verify_correctness verify_scope verify_risk}"
REVERT="${REVERT:-0}"

cd "$(dirname "$0")/../../.."
PY="${AIFORGE_PY:-.venv/bin/python}"
[[ -x "$PY" ]] || PY=python3

ROLES="$ROLES" JUDGE_MODEL="$JUDGE_MODEL" REVERT="$REVERT" "$PY" - << 'EOF'
import os
from aiforge_core.config import agent_config as ac

roles = os.environ["ROLES"].split()
revert = os.environ.get("REVERT") == "1"
model = (ac._local_default_model() if revert
         else os.environ["JUDGE_MODEL"])
for role in roles:
    row = ac.set_role(role, "local", model)
    print(f"{role}: {row['provider']} / {row['model']}")
print("done — restart aiforge-api/graph-runner is NOT needed "
      "(agent_config.json is read per ticket)")
EOF
