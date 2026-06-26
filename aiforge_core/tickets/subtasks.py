"""Internal subtask tracking for a ticket — NO separate child tickets.

When the Planner decomposes a big ticket into a ``subtickets`` array, we record
those subtasks INSIDE the parent ticket and track each one's status as the
agents work through them. The UI reads this to chart the breakdown + progress.

Event-sourced (no schema/metadata mutation): a ``subtasks_planned`` event holds
the initial list; each ``subtask_update`` event flips one subtask's status. The
current state is the planned list with the latest update per slug applied. Each
subtask: ``{slug, goal, status}`` where status ∈
pending | running | done | failed | skipped.
"""
from __future__ import annotations

from typing import Any

from . import store

_VALID = {"pending", "running", "done", "failed", "skipped"}


def set_subtasks(ticket_id: int, items: list[dict], *,
                 role: str | None = "planner") -> list[dict]:
    """Record the planned subtasks (status defaults ``pending``) as one
    ``subtasks_planned`` event. Returns the normalized list."""
    subs: list[dict] = []
    for i, it in enumerate(items or []):
        if not isinstance(it, dict):
            continue
        slug = str(it.get("slug") or it.get("id") or f"sub-{i + 1}").strip()
        # Normalize + validate status so a planner emitting "Done"/"In-Progress"
        # doesn't leak an unknown status that breaks progress() counting.
        status = str(it.get("status") or "pending").strip().lower()
        if status not in _VALID:
            status = "pending"
        subs.append({
            "slug": slug,
            "goal": str(it.get("goal") or it.get("title") or slug)[:300],
            "status": status,
        })
    if not subs:
        return []
    store.add_event(ticket_id, role, "subtasks_planned",
                    f"decomposed into {len(subs)} subtasks", {"subtasks": subs})
    return subs


def update_subtask(ticket_id: int, slug: str, status: str, *,
                   role: str | None = None) -> dict:
    """Flip one subtask's status via a ``subtask_update`` event."""
    status = (status or "").strip().lower()
    if status not in _VALID:
        return {"ok": False, "error": f"status must be one of {sorted(_VALID)}"}
    slug = (slug or "").strip()
    if not slug:
        return {"ok": False, "error": "missing 'slug'"}
    store.add_event(ticket_id, role, "subtask_update",
                    f"{slug} → {status}", {"slug": slug, "status": status})
    return {"ok": True, "subtasks": get_subtasks(ticket_id)}


def _events(ticket_id: int) -> list[dict]:
    try:
        return store.comments(ticket_id, 1000)
    except Exception:  # noqa: BLE001
        return []


def get_subtasks(ticket_id: int) -> list[dict]:
    """Reconstruct current subtask state: the latest planned list with each
    ``subtask_update`` applied in order. Empty when none planned."""
    planned: list[dict] = []
    updates: list[tuple[str, str]] = []
    for ev in _events(ticket_id):          # ascending order
        kind = ev.get("kind")
        md = ev.get("metadata") if isinstance(ev.get("metadata"), dict) else {}
        if kind == "subtasks_planned" and isinstance(md.get("subtasks"), list):
            planned = [dict(s) for s in md["subtasks"] if isinstance(s, dict)]
            updates = []                   # a new plan resets prior updates
        elif kind == "subtask_update" and md.get("slug"):
            updates.append((str(md["slug"]), str(md.get("status") or "pending")))
    by_slug = {s.get("slug"): s for s in planned}
    for slug, status in updates:
        if slug in by_slug:
            by_slug[slug]["status"] = status
        else:
            planned.append({"slug": slug, "goal": slug, "status": status})
            by_slug[slug] = planned[-1]
    return planned


def progress(subs: list[dict]) -> dict:
    """Counts by status + a done fraction, for the UI chart."""
    counts: dict[str, int] = {}
    for s in subs:
        st = (s.get("status") if isinstance(s, dict) else None) or "pending"
        counts[st] = counts.get(st, 0) + 1
    total = len(subs)
    done = counts.get("done", 0) + counts.get("skipped", 0)
    return {"total": total, "done": done, "counts": counts,
            "fraction": (done / total) if total else 0.0}


__all__ = ["set_subtasks", "update_subtask", "get_subtasks", "progress"]
