"""How tickets and their events are shaped for the API: timestamps, durations,
and the row dictionaries the UI reads."""
from __future__ import annotations

from datetime import UTC

from aiforge_core.config import env as _cfg

_TERMINAL = {"done", "cancelled"}


def _as_utc(ts):
    from datetime import datetime
    if ts is None:
        return datetime.now(UTC)
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts


def _duration_s(started, completed, status) -> float | None:
    """Seconds from start to completion — or to NOW while the run is still
    going. None until it has started."""
    if started is None:
        return None
    end = completed if (completed and status in _TERMINAL) else None
    return max(0.0, (_as_utc(end) - _as_utc(started)).total_seconds())


def _iso(ts):
    return ts.isoformat() if ts else None


def _ticket_row_out(r: dict) -> dict:
    started, completed = r.get("started_at"), r.get("completed_at")
    return {
        "id": r["id"], "identifier": r["identifier"], "title": r["title"],
        "body": r["body"], "status": r["status"], "priority": r["priority"],
        "assignee_role": (_cfg.canonical_role(r["assignee_role"])
                          if r.get("assignee_role") else None),
        "active_role": r.get("active_role"),
        "parent_id": r["parent_id"],
        "branch": r["branch"], "project": r["project"],
        "labels": list(r["labels"] or []),
        "metadata": dict(r["metadata"] or {}),
        "created_at": _iso(r.get("created_at")),
        "updated_at": _iso(r["updated_at"]),
        "completed_at": _iso(completed),
        "started_at": _iso(started),
        "duration_s": _duration_s(started, completed, r.get("status")),
        "route": r.get("route") or "code",
        "route_workflow": r.get("route_workflow"),
        "route_source": r.get("route_source") or "auto",
        "route_confidence": r.get("route_confidence"),
    }


def _event_row_out(r: dict) -> dict:
    return {
        "id": r["id"], "ticket_id": r["ticket_id"],
        "agent_role": r["agent_role"], "kind": r["kind"],
        "body": r["body"] or "",
        "metadata": dict(r["metadata"] or {}),
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
    }
