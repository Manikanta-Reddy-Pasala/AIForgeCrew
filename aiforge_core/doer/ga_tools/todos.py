"""Doer in-loop checklist — mirrors Claude Code's TodoWrite tool.

Lets the Doer track progress on multi-step tasks within a single GA
run. Keeps state on the handler (``handler._todos``) — no Postgres,
no DB. Each item: ``{id, text, status}``. Status = ``pending |
in_progress | completed``.

Two tools:
- ``todo_write(items=[{text, status?}])`` — replace whole list.
- ``todo_check(id, status)`` — flip one item's status.

After every write, the next-prompt nudge re-renders the current list
so the model always sees what remains. Toggle via
``AIFORGE_DOER_TODOS=1``.
"""
from __future__ import annotations


SCHEMA_WRITE = {
    "type": "function",
    "function": {
        "name": "todo_write",
        "description": (
            "Replace the in-loop checklist with a fresh ordered list. "
            "Call once at task start with the breakdown; call again "
            "only when scope changes meaningfully."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string"},
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed"],
                                "default": "pending",
                            },
                        },
                        "required": ["text"],
                    },
                },
            },
            "required": ["items"],
        },
    },
}


SCHEMA_CHECK = {
    "type": "function",
    "function": {
        "name": "todo_check",
        "description": "Flip one checklist item's status by id (1-indexed).",
        "parameters": {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "status": {
                    "type": "string",
                    "enum": ["pending", "in_progress", "completed"],
                },
            },
            "required": ["id", "status"],
        },
    },
}


def write(handler: object, items: list[dict]) -> str:
    """Replace the handler's checklist. Returns rendered list."""
    cleaned: list[dict] = []
    for i, raw in enumerate(items[:30], start=1):  # 30 cap
        if not isinstance(raw, dict):
            continue
        text = (raw.get("text") or "").strip()[:300]
        if not text:
            continue
        status = (raw.get("status") or "pending").strip().lower()
        if status not in ("pending", "in_progress", "completed"):
            status = "pending"
        cleaned.append({"id": i, "text": text, "status": status})
    handler._todos = cleaned  # type: ignore[attr-defined]
    return render(cleaned)


def check(handler: object, item_id: int, status: str) -> str:
    """Flip one item's status. Returns rendered list."""
    todos: list[dict] = list(getattr(handler, "_todos", []) or [])
    if status not in ("pending", "in_progress", "completed"):
        return f"[todos] invalid status {status!r}; use pending|in_progress|completed"
    found = False
    for it in todos:
        if it.get("id") == item_id:
            it["status"] = status
            found = True
            break
    handler._todos = todos  # type: ignore[attr-defined]
    if not found:
        return f"[todos] no item with id={item_id}; current list:\n{render(todos)}"
    return render(todos)


def render(todos: list[dict] | None) -> str:
    """Pretty-print the checklist with status icons."""
    if not todos:
        return "[todos] (empty)"
    icon = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]"}
    lines = [
        f"  {icon.get(t['status'], '[ ]')} {t['id']}. {t['text']}"
        for t in todos
    ]
    return "[todos]\n" + "\n".join(lines)


def render_for_handler(handler: object) -> str:
    """Snapshot of current checklist for prompt injection. Empty
    string when not initialised so callers can omit the section."""
    todos = getattr(handler, "_todos", None)
    if not todos:
        return ""
    return render(todos)
