"""Lean Simple chat: the lighter loop, closer to how OpenCode works.

Fixed scoreboard (11-09): OpenCode, same model, one plain loop (the model,
9 tools, no finish checks), scored 8/8 on three new features in 2-5 min;
AIForge with its extra model-call steps scored 3.5-7 of 8 and took 5-28 min.
Those steps re-ask the model what the code already shows, add calls and grow
the prompt.

Lean mode turns off the steps that only ASK the model again:
- the enhancer's restatement of the request (chat path),
- the "say what you are about to do" hold before the first change,
- the done check, the review pass and the spec audit at the finish.

It keeps everything that RUNS something or protects the work: the request's
examples and imports run before the answer, post-edit error checks, the test
run at the end, loop notes, waiting on long-running jobs, goals, the plan
panel, tasks, jobs and integrations.

On by default; ``AIFORGE_CHAT_LEAN=0`` restores the full flow. Each switch is
set only when the operator has not set it (``setdefault``).
"""
from __future__ import annotations

import os

#: The switches lean mode turns off (each step's own off-switch).
LEAN_OFF = {
    "AIFORGE_CHAT_SAY_PLAN": "0",
    "AIFORGE_CHAT_DONE_CHECK": "0",
    "AIFORGE_CHAT_REVIEW_PASS": "0",
    "AIFORGE_CHAT_SPEC_AUDIT": "0",
}


def on() -> bool:
    return os.environ.get("AIFORGE_CHAT_LEAN", "1").strip().lower() not in (
        "0", "false", "no", "off")


def apply() -> None:
    """Set the lean defaults for this process (an explicit setting wins)."""
    if not on():
        return
    for key, value in LEAN_OFF.items():
        os.environ.setdefault(key, value)
