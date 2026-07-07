"""Pre-pipeline clarification gate for interactive (chat) runs.

Tickets run static (no questions). Chat-created tickets carry
``metadata.interactive=True``; for those, before the pipeline runs we
ask the model whether the request is clear enough. If not, we record
the questions, park the ticket in ``blocked`` with
``metadata.awaiting_input=True``, and halt — the chat surfaces the
questions and the user's answer (folded into the body) re-queues the
ticket. On the second pass ``metadata.clarified=True`` so it proceeds.

Soft by design: any error → proceed (never wedge a run on the gate).
"""
from __future__ import annotations

import logging
import os

from aiforge_core.tickets import store as tickets_mod

log = logging.getLogger("aiforge.clarify")

try:
    # A body with at least this many words is already substantive — the local
    # triage model over-asks (it rarely emits a bare "CLEAR"), so a detailed
    # ticket would get spuriously parked in `blocked`. Skip the gate for those.
    _MIN_THIN_WORDS = max(1, int(os.environ.get("AIFORGE_CLARIFY_MIN_WORDS", "30")))
except (TypeError, ValueError):
    _MIN_THIN_WORDS = 30

_SYS = (
    "You are a triage clarifier for a coding pipeline. Decide if the "
    "request below is specific enough to implement without guessing.\n"
    "If it IS clear, reply with exactly: CLEAR\n"
    "If NOT, reply with up to 3 short clarifying questions, one per line, "
    "no numbering, no preamble. Ask only what materially changes the "
    "implementation."
)


def _interactive(ticket) -> bool:
    return bool((ticket.metadata or {}).get("interactive"))


def _clarified(ticket) -> bool:
    return bool((ticket.metadata or {}).get("clarified"))


def _thin(ticket) -> bool:
    """A title-only / one-liner ticket is worth a clarifying question; a
    ticket that already carries a substantive body (>= _MIN_THIN_WORDS words)
    is not — the local triage model over-asks, spuriously parking good
    tickets in ``blocked``. Only thin tickets go through the LLM gate."""
    body = (getattr(ticket, "body", "") or "").strip()
    return len(body.split()) < _MIN_THIN_WORDS


def _ambiguous_candidates(ticket) -> list[str]:
    """Rule names that scored an unresolved near-tie against this ticket's
    title+body — extra signal for the clarity check below. Soft-fail → []."""
    try:
        from aiforge_core.runtime import repo_rules
        import os
        query = f"{getattr(ticket, 'title', '') or ''}\n{getattr(ticket, 'body', '') or ''}"
        md = getattr(ticket, "metadata", None) or {}
        globs = md.get("scope_allowlist_globs") or []
        if isinstance(globs, str):
            globs = [g.strip() for g in globs.splitlines() if g.strip()]
        from aiforge_core.runtime.request_context import get_repo_root
        _, ambiguous = repo_rules.collect_or_ask(
            get_repo_root() or "", globs, query)
        return [" or ".join(f"'{r.name}'" for r in group)
               for group in ambiguous]
    except Exception:  # noqa: BLE001
        return []


def _ask_llm(ticket, ambiguous: list[str] | None = None) -> list[str]:
    from aiforge_core.llm.client import complete
    user = f"Title: {ticket.title}\n\nRequest:\n{ticket.body or ''}"
    if ambiguous:
        user += ("\n\nNote: these repo rules matched with near-equal "
                 "confidence and could not be auto-selected: "
                 + "; ".join(ambiguous) + ". If it matters to the "
                 "implementation, ask which one applies.")
    out = complete("triage", [
        {"role": "system", "content": _SYS},
        {"role": "user", "content": user},
    ], temperature=0.0, max_tokens=400) or ""
    text = out.strip()
    if not text or text.upper().startswith("CLEAR"):
        return []
    qs = []
    for line in text.splitlines():
        q = line.strip().lstrip("-*0123456789. ").strip()
        if q and "?" in q:
            qs.append(q)
    return qs[:3]


def maybe_clarify(ticket) -> bool:
    """Return True if the run was HALTED to ask the user (caller should
    stop processing this ticket); False to proceed with the pipeline."""
    if not _interactive(ticket) or _clarified(ticket):
        return False
    if not _thin(ticket):
        # Body is already substantive — don't run the over-eager gate; proceed.
        tickets_mod.update_status(ticket.id, "in_progress", role="clarify",
                                  metadata_patch={"clarified": True})
        return False
    try:
        ambiguous = _ambiguous_candidates(ticket)
        questions = _ask_llm(ticket, ambiguous)
    except Exception as exc:  # noqa: BLE001
        log.warning("clarify.skip ticket=%s err=%s", ticket.identifier, exc)
        return False
    if not questions:
        # clear enough — mark so we never re-ask, then proceed.
        tickets_mod.update_status(ticket.id, "in_progress", role="clarify",
                                  metadata_patch={"clarified": True})
        return False
    body = "I need a bit more detail before I start:\n" + "\n".join(
        f"- {q}" for q in questions)
    tickets_mod.add_event(ticket.id, "clarify", "clarification", body,
                          {"questions": questions})
    tickets_mod.update_status(
        ticket.id, "blocked", role="clarify",
        metadata_patch={"awaiting_input": True, "clarify_questions": questions},
    )
    # Surface the REASON in chat — otherwise the ticket just shows `blocked`
    # with no explanation. The user's next chat message answers the questions.
    sid = (ticket.metadata or {}).get("chat_session_id")
    if sid:
        try:
            from aiforge_core.runtime import chat_store
            chat_store.add_message(
                sid, "assistant", body,
                [{"type": "clarify", "ticket": ticket.identifier,
                  "questions": questions}])
        except Exception as exc:  # noqa: BLE001
            log.warning("clarify.chat_writeback ticket=%s err=%s",
                        ticket.identifier, exc)
    log.info("clarify.asked ticket=%s n=%d", ticket.identifier, len(questions))
    return True
