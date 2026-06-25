"""Ticket + ticket_event CRUD — backend-agnostic.

Source of truth for work items. All business logic (supervisor
invariants, priority claiming, status-change event writes, validation,
the ONE-<n> counter) lives here; the raw SQL lives in a storage backend
(SQLite by default, Postgres when AIFORGE_PG_URL is set) selected by
:mod:`aiforge_core.tickets.backend_factory`.

Public surface:
    new_identifier()                 -> str            # atomic ONE-<n>
    create(title, body, ...)         -> Ticket
    get(identifier | id)             -> Ticket
    claim_next_any()                 -> Ticket | None  # oldest todo across all roles
    update_status(id, status, ...)   -> Ticket
    add_comment(id, role, body)      -> int
    add_event(id, role, kind, body, metadata) -> int
    children(parent_id)              -> list[Ticket]
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime

from aiforge_core.tickets.backend_factory import get_backend


# "qa" = merged/awaiting QA verification (in-flight). "qa_failed" = QA
# rejected; terminal end-state, reopened manually by an operator.
VALID_STATUS = {
    "todo", "in_progress", "in_review", "qa", "qa_failed",
    "done", "blocked", "cancelled",
}
VALID_PRIORITY = {"low", "medium", "high", "urgent"}

# Statuses considered "active" for duplicate-title detection (excludes
# cancelled). Matches the historical by_title_project filter.
_ACTIVE_STATUSES = [
    "todo", "in_progress", "in_review", "qa", "qa_failed", "blocked", "done",
]

# Statuses that stamp completed_at.
_COMPLETED_STATUSES = {"done", "cancelled", "qa_failed"}


@dataclass
class Ticket:
    id: int
    identifier: str
    title: str
    body: str
    status: str
    priority: str
    assignee_role: str | None
    parent_id: int | None
    branch: str | None
    project: str | None
    labels: list[str]
    metadata: dict
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    route: str = "code"                       # 'code' or 'workflow'
    route_workflow: str | None = None         # workflow id when route='workflow'
    route_source: str = "auto"                # 'auto' or 'manual'
    route_confidence: float | None = None     # 0..1; null = unscored

    @classmethod
    def from_row(cls, r: dict) -> "Ticket":
        return cls(
            id=r["id"], identifier=r["identifier"], title=r["title"], body=r["body"],
            status=r["status"], priority=r["priority"],
            assignee_role=r["assignee_role"], parent_id=r["parent_id"],
            branch=r["branch"], project=r["project"],
            labels=list(r["labels"] or []), metadata=dict(r["metadata"] or {}),
            created_at=r["created_at"], updated_at=r["updated_at"],
            completed_at=r["completed_at"],
            route=r.get("route") or "code",
            route_workflow=r.get("route_workflow"),
            route_source=r.get("route_source") or "auto",
            route_confidence=r.get("route_confidence"),
        )


def new_identifier() -> str:
    """Atomic ONE-<n> allocator (counter seeded at 100)."""
    return f"ONE-{get_backend().next_counter()}"


_DANGEROUS_PATTERNS = [
    "drop table", "rm -rf /", "rm -rf ~", "delete all", "truncate table",
    "shutdown -h", "mkfs", "format c:", "> /dev/sda",
]
_URGENT_KEYWORDS = ["prod", "outage", "crash", "p0", "urgent", "incident"]


def _apply_supervisor_invariants(
    title: str, body: str, assignee_role: str | None,
    priority: str, labels: list[str] | None, metadata: dict | None,
) -> tuple[str | None, str, list[str], dict]:
    """Enforce hard safety + routing invariants at ticket-create time.
    Supervisor LLM still runs for the creative decisions; these are the
    floor that prevents dangerous work from being auto-routed."""
    labels = list(labels or [])
    metadata = dict(metadata or {})
    lower_body = f"{title}\n{body}".lower()

    # Dangerous intent → force supervisor review, never auto-route.
    if any(pat in lower_body for pat in _DANGEROUS_PATTERNS):
        assignee_role = "supervisor"
        if "review-required" not in labels:
            labels.append("review-required")
        metadata["dangerous_pattern"] = True

    # Auto priority-boost on urgent keywords.
    if priority not in ("urgent",) and any(kw in lower_body for kw in _URGENT_KEYWORDS):
        priority = "urgent"
        metadata["priority_auto_boosted"] = True

    # Default assignee → supervisor for triage.
    if assignee_role is None:
        assignee_role = "supervisor"

    return assignee_role, priority, labels, metadata


def create(
    *,
    title: str,
    body: str = "",
    assignee_role: str | None = None,
    parent_id: int | None = None,
    priority: str = "medium",
    project: str | None = None,
    labels: list[str] | None = None,
    branch: str | None = None,
    metadata: dict | None = None,
    identifier: str | None = None,
    route: str = "code",
    route_workflow: str | None = None,
    route_source: str = "auto",
    route_confidence: float | None = None,
) -> Ticket:
    if priority not in VALID_PRIORITY:
        raise ValueError(f"bad priority {priority!r}")
    if route not in ("code", "workflow"):
        raise ValueError(f"bad route {route!r}; expected 'code' or 'workflow'")
    if route == "workflow" and not route_workflow:
        raise ValueError("route='workflow' requires route_workflow id")
    if route_source not in ("auto", "manual"):
        raise ValueError(f"bad route_source {route_source!r}")
    # Children inherit their parent's assignee if caller didn't pick — DON'T
    # send them through supervisor triage again.
    if parent_id is None:
        assignee_role, priority, labels, metadata = _apply_supervisor_invariants(
            title, body, assignee_role, priority, labels, metadata,
        )
    else:
        labels = list(labels or [])
        metadata = dict(metadata or {})
    ident = identifier or new_identifier()
    row = get_backend().insert_ticket({
        "identifier": ident, "title": title, "body": body, "priority": priority,
        "assignee_role": assignee_role, "parent_id": parent_id, "project": project,
        "labels": labels or [], "branch": branch, "metadata": metadata,
        "route": route, "route_workflow": route_workflow,
        "route_source": route_source, "route_confidence": route_confidence,
    })
    return Ticket.from_row(row)


def update_route(
    ident_or_id: str | int,
    *,
    route: str,
    route_workflow: str | None = None,
    route_source: str = "manual",
    route_confidence: float | None = None,
) -> Ticket | None:
    """Override the route of an existing ticket. Used by the UI's
    'override' action — defaults source to 'manual' so audits stay clean.
    """
    if route not in ("code", "workflow"):
        raise ValueError(f"bad route {route!r}")
    if route == "workflow" and not route_workflow:
        raise ValueError("route='workflow' requires route_workflow id")
    if route_source not in ("auto", "manual"):
        raise ValueError(f"bad route_source {route_source!r}")
    row = get_backend().set_route(
        ident_or_id, route, route_workflow, route_source, route_confidence,
    )
    return Ticket.from_row(row) if row else None


def get(ident_or_id: str | int) -> Ticket | None:
    row = get_backend().fetch_ticket(ident_or_id)
    return Ticket.from_row(row) if row else None


_ROLE_ALIASES = {
    "supervisor":  ["supervisor", "architect"],
    "planner":     ["planner", "sr_developer"],
    "doer":        ["doer", "developer"],
    "learner":     ["learner", "fact_extract"],
    "feedback":    ["feedback"],
}


def _aliases_for(role: str) -> list[str]:
    """Return canonical role + any legacy names that should also match."""
    if role in _ROLE_ALIASES:
        return _ROLE_ALIASES[role]
    for canonical, aliases in _ROLE_ALIASES.items():
        if role in aliases:
            return aliases
    return [role]


def _excluded_projects() -> list[str]:
    """Projects the runner must NOT auto-claim. TallyConnector needs a
    Windows + COM environment the Linux runner can't provide, so those
    tickets are handled out-of-band (operator / Windows-side Claude)
    and left in ``todo`` for visibility. Override / extend via
    ``AIFORGE_RUNNER_EXCLUDE_PROJECTS`` (comma-separated)."""
    raw = os.environ.get(
        "AIFORGE_RUNNER_EXCLUDE_PROJECTS",
        "TallyConnector,Tally Connector",
    )
    return [p.strip() for p in raw.split(",") if p.strip()]


def claim_next_any() -> Ticket | None:
    """Atomically claim the oldest todo ticket across all roles.

    Priority-ordered (urgent>high>medium>low), then created_at. Marks
    status='in_progress' in the same transaction and records a
    status_change event. Tickets whose ``project`` is in
    :func:`_excluded_projects` (TallyConnector by default) are NEVER
    claimed. A NULL project is always claimable. Returns None when no
    eligible todo tickets exist.
    """
    row = get_backend().claim_oldest(_excluded_projects())
    if row is None:
        return None
    get_backend().insert_event(row["id"], "graph_runner", "status_change",
                               "in_progress", {})
    return Ticket.from_row(row)


def update_status(ticket_id: int, status: str, *, role: str | None = None,
                  metadata_patch: dict | None = None) -> Ticket | None:
    if status not in VALID_STATUS:
        raise ValueError(f"bad status {status!r}")
    row = get_backend().set_status(
        ticket_id, status, status in _COMPLETED_STATUSES, metadata_patch or {},
    )
    if row is None:
        # Unknown ticket — set_status found nothing. Writing the event
        # anyway hits the ticket_events→tickets FK and raises IntegrityError,
        # crashing the caller. Return None cleanly instead.
        return None
    get_backend().insert_event(ticket_id, role, "status_change", status, {})
    return Ticket.from_row(row)


def delete(ident_or_id: "str | int") -> bool:
    """Delete a ticket (and its events). Resolves an identifier (ONE-100)
    or numeric id. Returns True when a row was removed. Worktree / branch /
    PR are intentionally left untouched."""
    t = get(ident_or_id)
    if t is None:
        return False
    return get_backend().delete_ticket(t.id)


def reset_all() -> int:
    """Delete ALL tickets + events and reset the ONE-<n> counter so the next
    ticket starts the sequence over. Returns the count deleted."""
    return get_backend().reset_all_tickets()


def add_comment(ticket_id: int, role: str | None, body: str,
                metadata: dict | None = None) -> int:
    return add_event(ticket_id, role, "comment", body, metadata)


def add_event(ticket_id: int, role: str | None, kind: str, body: str | None,
              metadata: dict | None = None) -> int:
    return get_backend().insert_event(ticket_id, role, kind, body, metadata or {})


def children(parent_id: int) -> list[Ticket]:
    return [Ticket.from_row(r) for r in get_backend().fetch_children(parent_id)]


def by_title_project(title: str, project: str | None) -> list[Ticket]:
    """Find tickets with same lower(title) in the same project (active only)."""
    if not title:
        return []
    needle = title.strip().lower()
    rows = get_backend().search_title(needle, project, _ACTIVE_STATUSES)
    return [Ticket.from_row(r) for r in rows]


def comments(ticket_id: int, limit: int = 50) -> list[dict]:
    return get_backend().fetch_events(ticket_id, limit)


def list_tickets(role: str | None = None, statuses: list[str] | None = None,
                 parent_identifier: str | None = None,
                 limit: int = 100) -> list[dict]:
    """Enriched ticket list (started_at + active_role) for the dashboard.

    Returns raw dict rows; the API layer formats them. Backend-agnostic.
    """
    return get_backend().list_tickets(role, statuses, parent_identifier, limit)


def get_enriched(identifier: str) -> dict | None:
    """Single enriched ticket row by identifier (started_at + active_role)."""
    return get_backend().get_enriched(identifier)


def set_branch(ticket_id: int, branch: str) -> None:
    get_backend().set_branch(ticket_id, branch)


def append_body(ticket_id: int, extra: str) -> Ticket | None:
    """Append text to a ticket body (used to fold in chat clarifications)."""
    row = get_backend().append_body(ticket_id, extra)
    return Ticket.from_row(row) if row else None
