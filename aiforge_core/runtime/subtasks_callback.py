"""Persist the Planner's decomposition as INTERNAL subtasks on the ticket.

When the Planner emits a ``subtickets`` array (mega-ticket rule), this
after-callback records them on the parent ticket (event-sourced — no separate
child tickets) so the UI can chart the breakdown + progress and the Doer can
flip each subtask's status as it works through them.

Wire as the Planner agent's ``after_agent_callback`` in ``runtime/pipeline.py``.
"""
from __future__ import annotations

import json
import logging
import os
import re

log = logging.getLogger("aiforge.subtasks_callback")

_JSON_OBJ = re.compile(r"\{.*\}", re.DOTALL)


def _extract_subtickets(plan: object) -> list[dict]:
    """Pull the ``subtickets`` array out of the planner output (a JSON string,
    a fenced JSON block in markdown, or an already-parsed dict)."""
    obj: object = plan
    if isinstance(plan, str):
        s = plan.strip()
        try:
            obj = json.loads(s)
        except Exception:  # noqa: BLE001
            m = _JSON_OBJ.search(s)
            if not m:
                return []
            try:
                obj = json.loads(m.group(0))
            except Exception:  # noqa: BLE001
                return []
    if not isinstance(obj, dict):
        return []
    subs = obj.get("subtickets")
    return [s for s in subs if isinstance(s, dict)] if isinstance(subs, list) else []


def make_planner_subtasks_callback():
    """ADK ``after_agent_callback`` for the Planner: record the decomposition."""
    async def _callback(*, callback_context, **_kw):
        if os.environ.get("AIFORGE_SUBTASKS_DISABLE", "0") in ("1", "true"):
            return None
        try:
            state = callback_context.state
            subs = _extract_subtickets(state.get("plan_md"))
            if not subs:
                return None
            ident = state.get("ticket_identifier", "")
            if not ident:
                return None
            from aiforge_core.tickets import store, subtasks
            t = store.get(ident)
            if t is None:
                return None
            subtasks.set_subtasks(t.id, subs, role="planner")
            log.info("subtasks.recorded ticket=%s count=%d", ident, len(subs))
        except Exception as exc:  # noqa: BLE001 — never break the pipeline
            log.warning("subtasks_callback.failed: %s", exc)
        return None
    return _callback


__all__ = ["make_planner_subtasks_callback"]
