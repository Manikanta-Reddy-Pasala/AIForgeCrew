"""Turning a chat transcript into durable FACTS — the distillation half of
:mod:`chat_okr`, which keeps the session bookkeeping (windows, offsets, locks).

Split out because they are different concerns and different failure modes: a
bug here means a bad fact, a bug there means a window folded twice or not at
all. Everything below is about what the model is asked for and what is accepted
back.
"""
from __future__ import annotations

import logging

log = logging.getLogger("aiforge.chat_okr")


_EXTRACT_SYS = (
    "You distil a chat session between an engineer and an AI assistant into "
    "ATOMIC durable knowledge items worth remembering across sessions. Keep "
    "ONLY meaningful content: decisions, conventions, learnings, gotchas, "
    "config/stack facts, tickets worked, and MEANINGFUL user inputs "
    "(preferences, corrections, instructions the user gave). DROP pleasantries, "
    "small talk, transient status, and anything trivial or already obvious. "
    "Each item is ONE concise sentence tagged with a kind:\n"
    "  learning          a general lesson (applies across projects)\n"
    "  project_learning  a lesson about ONE repository/service\n"
    "  topic_learning    a lesson about a cross-cutting theme/workflow\n"
    "  user_comment      a meaningful thing the USER said to keep (intent/preference)\n"
    "PRESERVE EXACT IDENTIFIERS verbatim — jira/issue keys (ONE-3), version "
    "numbers, file paths, commands, config values, ports, error codes. Never "
    "generalize away or reword an id.\n"
    "Every item MUST also carry:\n"
    "  subject   the ONE thing the item is about (service, file, command, "
    "ticket, setting) — never a pronoun\n"
    "  evidence  where it came from (a command, path, id, or what was observed)\n"
    "Each `text` must be a COMPLETE standalone sentence that still makes sense "
    "a year from now with no chat around it. NEVER emit a heading, a table row, "
    "a CLI usage fragment, a question, a request the user made, or a truncated "
    "line — if you cannot state it as a full claim about a named subject, omit "
    "it.\n"
    "Return an items list; empty if nothing durable was said."
)


def _extract(transcript: str, role: str) -> "list | None":
    """LLM → list of items (each ``.text`` + ``.kind``); **None on failure**.

    The empty list means "nothing durable in these turns" (this is also the
    MEANINGFUL-input filter — the prompt drops chit-chat); None means the model
    never answered. The caller must not advance the durable offset on None, or
    one provider hiccup silently marks a whole window as folded with zero
    captures.
    """
    try:
        from pydantic import BaseModel

        from aiforge_core.llm.structured import structured_complete
        from aiforge_core.memory.md_store import _role

        class SessionItem(BaseModel):
            # subject/evidence are REQUIRED by the prompt: a model that cannot
            # name what a line is about, or where it came from, was echoing a
            # heading rather than reporting a learning. Defaults keep a partial
            # response parseable — _valid_items() drops the incomplete rows.
            text: str = ""
            kind: str = "learning"
            subject: str = ""
            evidence: str = ""

        class SessionItems(BaseModel):
            items: list[SessionItem] = []

        msgs = [{"role": "system", "content": _EXTRACT_SYS},
                # NO extra truncation here: the caller sized the window and the
                # durable offset advances over exactly those turns, so a second,
                # smaller cap would mark turns folded that the model never saw.
                {"role": "user", "content": transcript}]

        def _run(r: str) -> list:
            res = structured_complete(
                r, msgs, SessionItems,
                max_tokens=_extract_max_tokens(r),
                max_retries=1, temperature=0.0)
            return list(getattr(res, "items", None) or [])

        try:
            items = _run(role)
        except Exception as exc:  # noqa: BLE001
            # The memory role may point at a model this box has not loaded, so
            # the call RAISES rather than returning nothing. Falling back only
            # on an empty answer would leave such a box unable to distil at all
            # — and _record_window_failure eventually force-advances the offset,
            # so those turns would never be revisited.
            if not _role.is_thinking_role(role):
                raise
            log.info("chat_okr: %s failed (%s) — retrying on %s",
                     role, exc, _role.fallback_role())
            return _run(_role.fallback_role())
        # A reasoning model can also burn its whole budget thinking and answer
        # with nothing (model_registry documents this).
        if not items and _role.is_thinking_role(role):
            fb = _role.fallback_role()
            log.info("chat_okr: %s returned no items — retrying on %s", role, fb)
            items = _run(fb)
        return items
    except Exception as exc:  # noqa: BLE001  # model down → retry next pass
        log.warning("chat_okr extract failed (offset not advanced): %s", exc)
        return None


def _extract_max_tokens(role: str) -> int:
    """Token budget for one extract. A thinking role needs headroom for the
    reasoning phase BEFORE the first item is emitted, or it truncates to
    nothing — the exact failure that made memory work fast-role-only."""
    from aiforge_core.memory.md_store import _role
    from aiforge_core.runtime.chat_okr import _int_env

    base = _int_env("AIFORGE_SESSION_COMPACT_MAX_TOKENS", 2000)
    if _role.is_thinking_role(role):
        return max(base, _int_env("AIFORGE_SESSION_COMPACT_THINK_TOKENS", 6000))
    return base


def _valid_items(items) -> list:
    """Items the distiller returned that are actually facts.

    The model is asked for subject+evidence and a standalone claim; anything
    missing one is a fragment it echoed out of the transcript. Dropping here
    keeps the scope-classify call (one per window) off junk as well.
    """
    from aiforge_core.memory.md_store import _fact

    out = []
    for it in items or []:
        text = (getattr(it, "text", "") or "").strip()
        if not text:
            continue
        ok, why = _fact.is_wellformed(text)
        if not ok:
            log.info("chat_okr: dropped item (%s): %r", "; ".join(why), text[:80])
            continue
        out.append(it)
    return out


__all__ = ["_EXTRACT_SYS", "_extract", "_extract_max_tokens", "_valid_items"]
