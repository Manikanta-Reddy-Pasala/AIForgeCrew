"""What a chat has going on outside a turn: watches, background commands and
scheduled jobs.

None of it holds a chat run (``chat_runs``), so the page showed the chat as
idle — no pulse, "Nothing running" in the progress panel, no Stop — while a
watcher was polling or a scheduled agent run was working in that very chat.
This lists that work for the panel and says whether any of it is busy now.
Read-only; never raises.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

log = logging.getLogger("aiforge.chat_background")


def _aware(ts) -> "str | None":
    """A stored local time (the stores write naive local ISO) with its offset,
    so the page shows it in the reader's own clock."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone().isoformat()
    except ValueError:
        return None


def _watch_title(kind: str, payload: dict) -> str:
    args = (payload or {}).get("args") or {}
    if kind == "gitlab":
        what = (args.get("pipeline_id") or args.get("id") or args.get("ref")
                or args.get("project") or "")
        return f"Watching a GitLab pipeline {what}".strip()
    cmd = " ".join(str(args.get("cmd") or "").split())[:100]
    until = str(args.get("until") or "").strip()
    return (f"Watching `{cmd}`" if cmd else "Watching") + (f" until {until}" if until else "")


def _bg_items(session_id) -> list:
    from aiforge_core.runtime import bg_work
    out = []
    for r in bg_work._running_for(session_id):
        try:
            payload = json.loads(r.get("payload") or "{}")
        except ValueError:
            payload = {}
        kind = str(r.get("kind") or "watch")
        if kind == "command":
            title = "Running `" + " ".join(str(payload.get("cmd") or "").split())[:100] + "`"
            item_kind = "command"
        else:
            title, item_kind = _watch_title(kind, payload), "watch"
        out.append({"kind": item_kind, "id": f"bg-{r.get('id')}", "title": title,
                    "state": "running", "since": _aware(r.get("created_at"))})
    return out


def _command_items(session_id, seen: set) -> list:
    """Live commands of the chat not already listed as a bg row (a handed-off
    command has both, under one id: ``bg-<row>``)."""
    from aiforge_core.runtime import cmd_jobs
    out = []
    for j in cmd_jobs.for_session(session_id):
        jid = str(j.get("id"))
        if jid in seen:
            continue
        cmd = j.get("cmd") or ""
        out.append({"kind": "command", "id": jid,
                    "title": f"Running `{cmd}`" if cmd else "Running a command",
                    "state": "running", "for_s": j.get("for_s")})
    return out


def _job_running(job: dict) -> bool:
    """A fire in flight: agent runs in this process, or any kind (script jobs
    too) claimed in the store."""
    from aiforge_core.jobs import scheduler
    return bool(scheduler.is_running(job["id"]) or job.get("run_status") == "running")


def _schedule_items(session_id) -> list:
    from aiforge_core.jobs import store
    now = store.now_iso()
    out = []
    for job in store.list_jobs():
        if str(job.get("session_id") or "") != str(session_id):
            continue
        running = _job_running(job)
        expired = bool(job.get("expires_at")) and str(job["expires_at"]) <= now
        if not running and (not job.get("enabled") or expired):
            continue
        out.append({"kind": "schedule", "id": f"job-{job['id']}",
                    "title": job.get("name") or job.get("ticket_title") or f"job {job['id']}",
                    "state": "running" if running else "scheduled",
                    "cron": job.get("cron"), "next_run_at": _aware(job.get("next_run_at")),
                    "last_run_at": _aware(job.get("last_run_at")),
                    "expires_at": _aware(job.get("expires_at")),
                    "last_error": (job.get("last_error") or "")[:200] or None})
    return out


def items(session_id) -> list:
    """Every piece of background work of the chat, busy ones first."""
    if session_id is None:
        return []
    out: list = []
    for part in (_bg_items, _schedule_items):
        try:
            out += part(session_id)
        except Exception as exc:  # noqa: BLE001
            log.debug("background %s skipped: %s", part.__name__, exc)
    try:
        out += _command_items(session_id, {i["id"] for i in out})
    except Exception as exc:  # noqa: BLE001
        log.debug("background commands skipped: %s", exc)
    return sorted(out, key=lambda i: i["state"] != "running")


def active(session_id) -> bool:
    """Something of the chat is working right now (a schedule waiting for its
    next slot is not)."""
    return any(i["state"] == "running" for i in items(session_id))


def active_sessions() -> set:
    """Chats with background work busy now, for the chat list's pulse —
    one pass over each store, not a query per chat."""
    out: set = set()
    try:
        from aiforge_core.runtime import bg_work
        con = bg_work._connect()
        try:
            out |= {r[0] for r in con.execute(
                "SELECT DISTINCT session_id FROM bg WHERE status='running'").fetchall()
                if r[0] is not None}
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001
        log.debug("background sessions skipped: %s", exc)
    try:
        from aiforge_core.jobs import store
        out |= {int(j["session_id"]) for j in store.list_jobs()
                if str(j.get("session_id") or "").isdigit() and _job_running(j)}
    except Exception as exc:  # noqa: BLE001
        log.debug("scheduled sessions skipped: %s", exc)
    try:
        from aiforge_core.runtime import cmd_jobs
        with cmd_jobs._LOCK:
            jobs = list(cmd_jobs._JOBS.values())
        out |= {int(j.session_id) for j in jobs
                if str(j.session_id or "").isdigit() and j.alive()}
    except Exception as exc:  # noqa: BLE001
        log.debug("command sessions skipped: %s", exc)
    return out


def stop(session_id) -> dict:
    """Stop the chat's background work only: its watches and background
    commands, and the scheduled run in flight — not a live turn, not the
    services or pane commands of the chat, not the schedule itself."""
    out = {"watches": 0, "scheduled_run": False}
    try:
        from aiforge_core.runtime import bg_work
        out["watches"] = bg_work.stop_session(session_id)
    except Exception as exc:  # noqa: BLE001
        log.debug("background stop skipped: %s", exc)
    try:
        from aiforge_core.jobs import scheduler
        out["scheduled_run"] = bool(scheduler.request_stop_session(session_id))
    except Exception as exc:  # noqa: BLE001
        log.debug("scheduled stop skipped: %s", exc)
    return out


__all__ = ["active", "active_sessions", "items", "stop"]
