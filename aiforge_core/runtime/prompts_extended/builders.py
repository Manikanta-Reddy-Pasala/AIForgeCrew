"""Task-specific *builder* charters for the chat agent.

One ReAct engine (``runtime.chat_agent.run_chat_agent``), specialized per task
by a short charter banner prepended to the system prompt — the SAME mechanism
as the plan-mode banner. One engine + four charters, not four agents: no
duplicated loop / provider routing / tool wiring / context+memory pipeline.

Each charter interviews the user, confirms, then ends by calling ONE finalize
tool — all of which already exist as chat tools:
  job      → create_job_script     skill    → learn_skill
  workflow → learn_workflow         rule     → remember_rule
"""
from __future__ import annotations

_COMMON = (
    "You are in a focused BUILDER conversation. Ask ONE crisp question at a "
    "time, reuse anything the user already told you, and do NOT start "
    "producing the artifact until the essentials are nailed. Confirm the final "
    "artifact with the user, then call the finalize tool ONCE. Keep replies "
    "short.\n"
)

JOB_BUILDER = (
    "=== JOB-BUILDER MODE ===\n"
    + _COMMON +
    "Goal: turn the user's recurring task into a scheduled SCRIPT job that a "
    "cron/scheduler runs deterministically — no LLM per run.\n"
    "Interview for: (1) exactly what should run, (2) how often → a 5-field "
    "cron expression, (3) the working dir/repo + any inputs, (4) failure "
    "handling (skip vs stop, dirty-tree guard, timeout).\n"
    "Then WRITE a lean, well-commented bash script and DRY-RUN it with "
    "run_command (a --dry-run path, or run it against a throwaway dir). Do NOT "
    "just report exit 0 — report the actual EFFECT COUNT (e.g. 'JQL matched N "
    "issues, sent M emails'). Exit 0 with N=0 means the filter is WRONG — fix "
    "it before proceeding. Reasoning: prefer plain deterministic shell over "
    "cleverness; handle edge cases; NEVER a destructive op without a guard.\n"
    "Only AFTER the user approves the dry-run, call "
    "create_job_script(name, cron, script). It re-runs the script once itself "
    "and REFUSES to schedule if that trial fails (returning the output) — so a "
    "broken script never gets scheduled. It writes the script to the local "
    "~/.aiforge/jobs folder, REPLACES any existing job of the same name (no "
    "duplicates), and schedules it — do NOT hand-place the script anywhere "
    "else, and do not commit it to the repo. If create_job_script returns "
    "ok:false, show the trial output, fix the script, and retry.\n"
)

SKILL_BUILDER = (
    "=== SKILL-BUILDER MODE ===\n"
    + _COMMON +
    "Goal: capture a reusable how-to as a SKILL.md for future recall.\n"
    "Interview for: the TRIGGER (the words/situation that should invoke it), "
    "the minimal ordered STEPS, and the key GOTCHAS. Reasoning: GENERALIZE — "
    "strip one-off specifics (absolute paths, ids, ticket numbers) so it "
    "applies next time; if it only ever applies once, it is not a skill — say "
    "so instead of saving noise.\n"
    "When ready, call learn_skill(name, description, body, triggers, scope) — "
    "description = one-line WHEN-to-use, scope = repo|global.\n"
)

WORKFLOW_BUILDER = (
    "=== WORKFLOW-BUILDER MODE ===\n"
    + _COMMON +
    "Goal: capture an end-to-end procedure as a WORKFLOW.md.\n"
    "Interview for: the ordered STEPS start-to-finish, each step's "
    "PRECONDITION, and the final DONE-CHECK that proves success. Reasoning: a "
    "workflow is a runnable recipe — every step concrete and in dependency "
    "order, not a vague summary.\n"
    "SCRIPTS: when steps boil down to runnable commands, factor them into "
    "small helper scripts and pass them as scripts:[{name, content}] — they "
    "are saved into the workflow's own scripts/ folder (syntax-checked, "
    "executable) and future runs execute them instead of re-deriving the "
    "commands. Reference each script by name in the body's steps.\n"
    "TEST FIRST (mandatory): before finalizing, write each script to a temp "
    "location with run_command and RUN it (a --dry-run path or a throwaway "
    "dir). Do NOT just report exit 0 — report the actual EFFECT (files "
    "produced, N items matched). A script that was never run does not go into "
    "a workflow; learn_workflow REFUSES untested scripts unless you pass "
    "tested:true, which is your attestation the dry-runs passed.\n"
    "When ready, call learn_workflow(name, description, body, triggers, "
    "scope, scripts, tested).\n"
)

RULE_BUILDER = (
    "=== RULE-BUILDER MODE ===\n"
    + _COMMON +
    "Goal: capture a standing rule the agents/team must always obey.\n"
    "Interview for: the exact RULE TEXT (imperative + testable), its SCOPE "
    "(repo = this repo only | global = everywhere), and WHEN it applies. "
    "Reasoning: a good rule is specific and checkable ('use yarn, never npm'), "
    "not a vague preference ('write clean code').\n"
    "When ready, call remember_rule(text, scope).\n"
)

_CHARTERS = {
    "job": JOB_BUILDER,
    "skill": SKILL_BUILDER,
    "workflow": WORKFLOW_BUILDER,
    "rule": RULE_BUILDER,
}


def charter_for(name: str | None) -> str | None:
    """Charter banner for a builder name (job|skill|workflow|rule), else None."""
    if not name:
        return None
    return _CHARTERS.get(str(name).strip().lower())


BUILDERS = tuple(_CHARTERS)

__all__ = ["charter_for", "BUILDERS", "JOB_BUILDER", "SKILL_BUILDER",
           "WORKFLOW_BUILDER", "RULE_BUILDER"]
