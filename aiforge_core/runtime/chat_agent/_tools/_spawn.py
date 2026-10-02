"""``spawn_task``: hand an independent part of the request to a second agent.

The server already runs side tasks (api/routes/_chat/_side_tasks): a child chat
in the same folder with its own run, whose answer is posted back here when it
finishes. This lets the agent start one itself for a part of the request that
does not depend on the rest, then carry on with its own part. A task that only
reads starts at once; one that edits waits while another run in the chat is
editing (the server serialises edits), so the useful split is research and
analysis from the part that changes files.
"""
from __future__ import annotations


def _t_spawn_task(args: dict, _cwd: str) -> dict:
    task = str(args.get("task") or "").strip()
    if len(task) < 12:
        return {"ok": False, "error": "spawn_task needs the full task text "
                "(what to do, with the paths and names it needs)"}
    try:
        from aiforge_core.runtime import chat_store, request_context
        raw = request_context.get_session_id()
        sid = int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        sid = 0
    if not sid:
        return {"ok": False, "error": "no chat session: do this part yourself"}
    try:
        session = chat_store.get_session(sid) or {}
        if session.get("parent_id"):
            return {"ok": False, "error": "this is already a side task: do "
                    "the work yourself instead of starting more"}
        from aiforge_core.api.routes._chat import _side_tasks
        mode = str(args.get("mode") or "simple").lower()
        child = _side_tasks.create(sid, task, mode if mode in ("simple", "plan") else "simple")
    except Exception as exc:  # noqa: BLE001 — a failed spawn means: do it yourself
        return {"ok": False, "error": f"could not start it ({exc}): do this part yourself"}
    return {"ok": True, "task_id": child.get("id"),
            "state": (child.get("task") or {}).get("state") or child.get("state"),
            "note": "it runs in its own chat and its answer is posted here when "
                    "it finishes. Do not wait for it: continue with your own "
                    "part of the request."}
