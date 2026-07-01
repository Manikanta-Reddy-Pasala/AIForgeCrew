"""workflows.select_or_ask must reuse skills.select_or_ask's scorer/
ambiguity logic against the workflow pool — same mechanism, different
folder, per repo_rules.py-adjacent design decision to unify skills/
workflows/rules disambiguation on one scorer."""
from __future__ import annotations

from aiforge_core.runtime import workflows as wf
from aiforge_core.runtime.skills import Skill


def test_workflows_select_or_ask_reuses_skills_scorer(monkeypatch):
    pool = [
        Skill(name="ship-staging", description="", triggers=("deploy", "staging"),
             body="staging steps", source="", always=False, priority=1),
        Skill(name="ship-prod", description="", triggers=("deploy", "prod"),
             body="prod steps", source="", always=False, priority=1),
    ]
    monkeypatch.setattr(wf, "load", lambda cwd=None: pool)
    monkeypatch.setenv("AIFORGE_AMBIGUITY_MARGIN", "0.15")
    monkeypatch.setenv("AIFORGE_AMBIGUITY_FLOOR", "2.0")
    chosen, ambiguous = wf.select_or_ask("deploy this now")
    assert len(ambiguous) == 1
    assert {s.name for s in ambiguous[0]} == {"ship-staging", "ship-prod"}
