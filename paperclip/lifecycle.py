"""Ticket lifecycle state machine per DESIGN.md §4 TDD flow.

Rule: one ticket, all agents comment on same ticket (no sub-tickets).
Transitions are routed by `advance()`; invalid transitions raise.
"""
from __future__ import annotations

from .config import PaperclipConfig
from .store import Store, TICKET_STATES

# Allowed transitions: (current_state) → set of allowed (next_state, next_assignee_key)
# Assignee keys reference routing fields in paperclip.config.yml.
_TRANSITIONS: dict[str, list[tuple[str, str | None]]] = {
    "created":        [("planning",       None)],                          # picked up by EM
    "planning":       [("tests_writing",  "post_planning")],               # EM → Tester
    "tests_writing":  [("coding",         "post_tests_ready")],            # Tester → Sr Dev
    "coding":         [("verifying",      "post_code_ready")],             # Sr Dev → Tester
    "verifying":      [("reviewing",      "post_verified"),                # pass → Architect
                       ("coding",         "post_tests_ready"),             # fail → back to Sr Dev (retry loop)
                       ("escalated",      None)],                          # retries exhausted
    "reviewing":      [("mr_created",     "on_approve"),                   # approve → human
                       ("coding",         "post_tests_ready"),             # reject → back to Sr Dev
                       ("escalated",      None)],
    "mr_created":     [("merged",         None),                           # human merges
                       ("escalated",      None)],
    "merged":         [],
    "escalated":      [],
}


class LifecycleError(RuntimeError):
    """Invalid transition or missing routing target."""


def allowed_next_states(current: str) -> list[str]:
    return [s for (s, _) in _TRANSITIONS.get(current, [])]


def route_assignee(cfg: PaperclipConfig, routing_key: str) -> str:
    mapping = {
        "post_planning":    cfg.routing.post_planning,
        "post_tests_ready": cfg.routing.post_tests_ready,
        "post_code_ready":  cfg.routing.post_code_ready,
        "post_verified":    cfg.routing.post_verified,
        "on_approve":       cfg.routing.on_approve,
    }
    try:
        return mapping[routing_key]
    except KeyError as e:
        raise LifecycleError(f"unknown routing key: {routing_key}") from e


def advance(store: Store, cfg: PaperclipConfig, ticket_id: str, next_state: str, actor: str) -> None:
    """Move a ticket to next_state (must be allowed); update assignee per routing."""
    t = store.get_ticket(ticket_id)
    if t is None:
        raise LifecycleError(f"ticket not found: {ticket_id}")

    transitions = _TRANSITIONS.get(t.state, [])
    routing_key = None
    for (s, rkey) in transitions:
        if s == next_state:
            routing_key = rkey
            break
    else:
        raise LifecycleError(
            f"invalid transition {t.state} → {next_state} "
            f"(allowed: {', '.join(allowed_next_states(t.state)) or 'none'})"
        )

    store.transition(ticket_id, next_state, actor)
    if routing_key is not None:
        new_assignee = route_assignee(cfg, routing_key)
        store.assign(ticket_id, new_assignee, actor)
