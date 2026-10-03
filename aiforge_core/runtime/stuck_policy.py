"""Every stuck / loop / recovery knob, read from the environment in ONE place.

``Policy.load()`` re-reads the environment on each call (a few ``os.environ``
lookups), so a test that patches an env var, or an operator who changes one
between turns, is honoured exactly as before. Detectors and the response
ladder never call ``os.environ`` themselves: they take a ``Policy``.

The env-var NAMES are the public contract and never change. A name that is
now only an alias of another is listed in ``ALIASES`` and still honoured.

This module is neutral on purpose (no ``chat_agent`` import): the chat loop,
the pipeline and ``llm.reasoning`` all read the same policy.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

#: Env names Policy honours, as {field: name}. The single source for docs,
#: the Settings screen and the "what can I tune" answer.
ENV = {
    "backstop": "AIFORGE_CHAT_LOOP_BACKSTOP",
    "max_recoveries": "AIFORGE_CHAT_MAX_RECOVERIES",
    "stuck_recoveries": "AIFORGE_CHAT_STUCK_RECOVERIES",
    "identical_repeats": "AIFORGE_CHAT_IDENTICAL_REPEATS",
    "monologue_repeats": "AIFORGE_CHAT_MONOLOGUE_REPEATS",
    "monologue_similarity": "AIFORGE_CHAT_MONOLOGUE_SIMILARITY",
    "pause_on_stuck": "AIFORGE_CHAT_PAUSE_ON_STUCK",
    "stuck_escalations": "AIFORGE_CHAT_STUCK_ESCALATIONS",
    "stuck_restart": "AIFORGE_CHAT_STUCK_RESTART",
    "context_error_restart": "AIFORGE_CHAT_CONTEXT_ERROR_RESTART",
    "goal_loop": "AIFORGE_CHAT_GOAL_LOOP",
    "no_progress_steps": "AIFORGE_NO_PROGRESS_STEPS",
    "same_failure_limit": "AIFORGE_SAME_FAILURE_LIMIT",
    "reason_steps": "AIFORGE_STUCK_REASON_STEPS",
    "tool_repeat_limit": "AIFORGE_TOOL_REPEAT_LIMIT",
    "plateau_replans": "AIFORGE_PLATEAU_REPLANS",
    "no_edit_iters": "AIFORGE_NO_EDIT_ITERS",
    "loop_max_wall_s": "AIFORGE_LOOP_MAX_WALL_S",
}

#: Old names that mean the same as a core one (honoured; none today).
ALIASES: dict[str, tuple[str, ...]] = {}

_OFF = ("0", "false", "no", "off")
_ON = ("1", "true", "yes", "on")


def _raw(field: str):
    for name in (ENV[field], *ALIASES.get(field, ())):
        val = os.environ.get(name)
        if val is not None:
            return val
    return None


def _int(field: str, default: int, *, floor: int | None = None,
         positive: bool = False) -> int:
    """An int knob: garbage gives ``default``; ``positive`` also sends 0 and
    below to ``default``; ``floor`` clamps up."""
    raw = _raw(field)
    try:
        val = int(default if raw is None else raw)
    except ValueError:
        return default
    if positive and val <= 0:
        return default
    return val if floor is None else max(floor, val)


def _pipeline_int(field: str, default: int) -> int:
    """The pipeline's parser: empty or garbage gives ``default``."""
    raw = _raw(field)
    try:
        return int(raw or default)
    except (TypeError, ValueError):
        return default


def _flag(field: str, default: bool) -> bool:
    raw = _raw(field)
    if raw is None:
        return default
    raw = raw.strip().lower()
    return raw not in _OFF if default else raw in _ON


@dataclass(frozen=True)
class Policy:
    #: repeats of one action since the last new workspace state that count as
    #: a loop (3x as many in the whole run always do)
    backstop: int = 30
    #: stuck recoveries one run may use between two task-board items done
    max_recoveries: int = 30
    #: recap nudges per stall (0 = legacy behaviour, no recovery nudge)
    stuck_recoveries: int = 3
    #: identical call + identical result in a row that count as a loop
    identical_repeats: int = 5
    #: replies in a row with no tool that say the same (0 = off)
    monologue_repeats: int = 3
    monologue_similarity: float = 0.85
    #: restore the old "stop and ask" instead of changing approach
    pause_on_stuck: bool = False
    #: change-of-approach trips before the wrap-up (0 = never)
    stuck_escalations: int = 20
    stuck_restart: bool = True
    context_error_restart: bool = True
    goal_loop: bool = True
    #: steps in a row with no progress of any kind (at least 8)
    no_progress_steps: int = 25
    #: distinct workspace states one failure may survive (at least 2)
    same_failure_limit: int = 3
    #: model calls a stall turns reasoning on for
    reason_steps: int = 6
    #: ADK-path identical tool calls (0 = off)
    tool_repeat_limit: int = 4
    plateau_replans: int = 2
    #: Doer iterations without a file change before a stall (0 = off, as 1e9)
    no_edit_iters: int = 2
    loop_max_wall_s: int = 0

    @classmethod
    def load(cls) -> "Policy":
        try:
            sim = float(_raw("monologue_similarity") or 0.85)
        except ValueError:
            sim = 0.85
        return cls(
            backstop=_int("backstop", 30, positive=True),
            max_recoveries=_int("max_recoveries", 30, positive=True),
            stuck_recoveries=_int("stuck_recoveries", 3, floor=0),
            identical_repeats=_int("identical_repeats", 5, positive=True),
            monologue_repeats=_int("monologue_repeats", 3, floor=0),
            monologue_similarity=min(1.0, max(0.5, sim)),
            pause_on_stuck=_flag("pause_on_stuck", False),
            stuck_escalations=_int("stuck_escalations", 20, floor=0),
            stuck_restart=_flag("stuck_restart", True),
            context_error_restart=_flag("context_error_restart", True),
            goal_loop=_flag("goal_loop", True),
            no_progress_steps=_int("no_progress_steps", 25, floor=8),
            same_failure_limit=_int("same_failure_limit", 3, floor=2),
            reason_steps=_int("reason_steps", 6, floor=0),
            tool_repeat_limit=_int("tool_repeat_limit", 4),
            plateau_replans=_pipeline_int("plateau_replans", 2),
            no_edit_iters=_pipeline_int("no_edit_iters", 2),
            loop_max_wall_s=_pipeline_int("loop_max_wall_s", 0),
        )


def load() -> Policy:
    return Policy.load()
