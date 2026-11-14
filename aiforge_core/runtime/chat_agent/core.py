"""Core mode: the loop leaves the model alone between its own steps.

Design (11-13). OpenCode runs a plain loop: the model, a tool, its result,
append-only history, one prompt that does not change. AIForge grew about fifty
layers on top, each added after one failed benchmark run: notes that steer the
model mid-work, refusals and narrowed tool lists, checks that re-open board
items, maps of the repository in the prompt, and a history wipe when it looks
stuck. The model spends calls answering the harness instead of the task.

Core mode keeps what the product needs and what protects the work:
- the progress panel: goals (and editing them), the task board, background
  jobs, the evidence under each task;
- safety: approvals, policy, refusing commands that would wipe the turn's work,
  deadlines and caps, waiting on long-running jobs;
- boundary checks: the spec section handed back when a task is closed;
- deterministic finish checks: the test run at the end, running the request's
  examples, the done watch, and the checks that keep the answer honest
  (claims of edits or files that did not change);
- memory recall and every integration (Jira, Confluence, GitLab, workflows).

It turns off the layers that talk to the model between its steps (each with
its own existing off switch). A layer comes back only when a measured run
shows it adds score.

Opt-in while it is measured: ``AIFORGE_CHAT_CORE=1``. Each switch is set only
when the operator has not set it (``setdefault``).
"""
from __future__ import annotations

import os

#: The layers core mode turns off, by their own off switches.
CORE_OFF = {
    # notes and refusals while the model works
    "AIFORGE_CHAT_PROBE_GUARD": "0",       # "many commands / reads and no edit"
    "AIFORGE_CHAT_FORCE_WRITE": "0",       # next call may only edit
    "AIFORGE_CHAT_STRUCTURE_CHECK": "0",   # moved/dropped functions note
    "AIFORGE_CHAT_TEST_GUARD": "0",        # changed old assertions note
    "AIFORGE_CHAT_STUB_NOTE": "0",         # stub functions note
    "AIFORGE_CHAT_TRACE_HINT": "0",        # line trace after a repeated failure
    "AIFORGE_CHAT_EXAMPLE_GATE": "0",      # "examples have no test" send-back
    # the board stays (panel); the harness stops re-opening and parking items
    "AIFORGE_CHAT_ITEM_VERIFY": "0",
    "AIFORGE_CHAT_ITEM_TIMEBOX_S": "0",    # also turns off parking (history reset)
    # AIFORGE_CHAT_SECTION_CHECK stays ON: it runs only when the model closes a
    # task (the section's own spec text rides on that "done" result), a
    # boundary check, not a note between steps. Click 11-14: core 5-6/37 in 3
    # runs, core + section check 18/37 at the same 25 min.
    # history and prompt stay the model's own
    "AIFORGE_CHAT_STUCK_RESTART": "0",     # wipe history every 2nd stuck trip
    "AIFORGE_CHAT_STUCK_CONDENSE": "0",    # condense history on every stuck trip
    "AIFORGE_CHAT_SKIP_REPEAT_READS": "0", # refuse a read already shown
    "AIFORGE_CHAT_AIDER_MAP": "0",         # repository map in the prompt
    "AIFORGE_CHAT_SYMBOL_MAP": "0",
}


def on() -> bool:
    return os.environ.get("AIFORGE_CHAT_CORE", "0").strip().lower() in (
        "1", "true", "yes", "on")


def apply() -> None:
    """Set the core defaults for this process (an explicit setting wins)."""
    if not on():
        return
    for key, value in CORE_OFF.items():
        os.environ.setdefault(key, value)
