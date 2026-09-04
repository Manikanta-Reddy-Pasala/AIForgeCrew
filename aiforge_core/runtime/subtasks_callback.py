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


def _parse_plan_obj(plan: object) -> object:
    """The planner output as a parsed object: a str is tried as JSON, then as a
    fenced JSON block; a dict is returned as-is; anything else unchanged. Returns
    the string itself when no JSON could be extracted (caller falls to phases)."""
    if not isinstance(plan, str):
        return plan
    s = plan.strip()
    try:
        return json.loads(s)
    except Exception:  # noqa: BLE001
        m = _JSON_OBJ.search(s)
        if not m:
            return plan
        try:
            return json.loads(m.group(0))
        except Exception:  # noqa: BLE001
            return plan


def _extract_subtickets(plan: object) -> list[dict]:
    """Pull the ``subtickets`` array out of the planner output (a JSON string, a
    fenced JSON block in markdown, or an already-parsed dict)."""
    obj = _parse_plan_obj(plan)
    if not isinstance(obj, dict):
        # A bare markdown plan with no JSON wrapper — still try the phases.
        return _phases_from_markdown(str(plan)) if isinstance(plan, str) else []
    subs = obj.get("subtickets")
    if isinstance(subs, list) and subs:
        out = [s for s in subs if isinstance(s, dict)]
        if out:
            return out
    # Fallback: the model often writes the breakdown as a NUMBERED MARKDOWN list
    # inside plan_md ("1. **Title** — desc") instead of a subtickets array. Parse
    # those phases so the subtask panel shows regardless of format.
    return _phases_from_markdown(str(obj.get("plan_md") or ""))


# "1. **Title** — description"  /  "2. Title: description"  /  "3) Title"
_PHASE_RE = re.compile(
    r"^\s*\d+[.)]\s+(?:\*\*(?P<t1>[^*]+?)\*\*|(?P<t2>[^:—\-\n]+?))"
    r"(?:\s*[—:\-]\s*(?P<desc>.+))?$",
    re.MULTILINE)


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:40] or "step"


# A numbered list item, capturing the leading number so we can detect a
# SECOND list (the planner often writes a summary list AND a detailed list —
# we want only the first one, not both concatenated).
_NUM_ITEM_RE = re.compile(
    r"^\s*(?P<n>\d+)[.)]\s+(?:\*\*(?P<t1>[^*]+?)\*\*|(?P<t2>[^:—\-\n]+?))"
    r"(?:\s*[—:\-]\s*(?P<desc>.+))?$")


def _phase_from_line(m, seen: set) -> "dict | None":
    """One subtask dict from a matched numbered-list line, or None to skip it
    (empty/overlong title, or a duplicate slug)."""
    title = (m.group("t1") or m.group("t2") or "").strip().strip("*` ")
    if not title or len(title) > 120:
        return None
    slug = _slugify(title)
    if slug in seen:
        return None
    seen.add(slug)
    desc = (m.group("desc") or "").strip().strip("*` ")
    return {"slug": slug, "goal": desc or title}


def _phases_from_markdown(md: str) -> list[dict]:
    """Extract the FIRST numbered list from a markdown plan body as subtasks.

    Stops when the numbering resets (n <= previous) — that's a second list (e.g.
    a detailed breakdown repeating the summary), which would otherwise duplicate
    every phase.
    """
    if not md:
        return []
    out: list[dict] = []
    seen: set = set()
    prev_n, started = 0, False
    for line in md.splitlines():
        m = _NUM_ITEM_RE.match(line)
        if not m:
            continue
        n = int(m.group("n"))
        if started and n <= prev_n:
            break                      # numbering reset → a second list; stop
        started, prev_n = True, n
        phase = _phase_from_line(m, seen)
        if phase is not None:
            out.append(phase)
            if len(out) >= 12:
                break
    # Require >=2 phases to treat it as a real decomposition (avoid a lone
    # numbered line in prose becoming a "1-subtask" plan).
    return out if len(out) >= 2 else []


def _record_planner_subtasks(state) -> None:
    """Record the Planner's decomposition on the ticket. Skips a no-op replan
    (same slugs already recorded — re-emitting would reset in-flight progress)."""
    subs = _extract_subtickets(state.get("plan_md"))
    if not subs:
        return
    ident = state.get("ticket_identifier", "")
    if not ident:
        return
    from aiforge_core.tickets import store, subtasks
    t = store.get(ident)
    if t is None:
        return
    new_slugs = [str(s.get("slug") or "").strip() for s in subs]
    cur_slugs = [s.get("slug") for s in subtasks.get_subtasks(t.id)]
    if cur_slugs and cur_slugs == new_slugs:
        return
    subtasks.set_subtasks(t.id, subs, role="planner")
    log.info("subtasks.recorded ticket=%s count=%d", ident, len(subs))


def make_planner_subtasks_callback():
    """ADK ``after_agent_callback`` for the Planner: record the decomposition."""
    def _callback(*, callback_context, **_kw):
        if os.environ.get("AIFORGE_SUBTASKS_DISABLE", "0") in ("1", "true"):
            return None
        try:
            _record_planner_subtasks(callback_context.state)
        except Exception as exc:  # noqa: BLE001 — never break the pipeline
            log.warning("subtasks_callback.failed: %s", exc)
        return None
    return _callback


__all__ = ["make_planner_subtasks_callback"]
