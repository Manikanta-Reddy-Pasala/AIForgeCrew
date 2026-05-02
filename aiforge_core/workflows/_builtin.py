"""Built-in workflow registrations.

Importing this module is the side-effect that populates :data:`REGISTRY`.
Add new workflows by appending more :func:`register` calls.
"""
from __future__ import annotations

from .registry import WorkflowSpec, register


# ── Tally ↔ OneShell trial-balance reconciliation ────────────────────
register(WorkflowSpec(
    id="tally-trial-balance",
    label="Tally ↔ OneShell trial-balance reconciliation",
    description=(
        "Validate, parse, and reconcile a Tally export against the "
        "OneShell ledger using either an attached OneShell file (2-way) "
        "or a live PCB /trialBalance API pull (3-way). Returns a "
        "Markdown audit report; blocks when material gaps are detected."
    ),
    handler="aiforge_core.aiforge_agents.processes.trial_balance:run_workflow",
    triggers={
        "keywords_any": [
            "trial balance", "trial-balance", "tally recon",
            "tally reconciliation", "ledger reconcile",
        ],
        "attachments_all": ["tally", "oneshell"],
        "intent_action_in": ["investigate", "audit", "test", "ops"],
        "min_confidence": 0.6,
    },
    required_attachments=["tally", "oneshell"],
    optional_inputs=["business_id", "env"],
    tags=["finance", "audit", "deterministic"],
))
