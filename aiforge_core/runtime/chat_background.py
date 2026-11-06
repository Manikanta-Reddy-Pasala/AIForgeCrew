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
        from aiforge_core.jobs import run_log
        runs = [{"started_at": _aware(r.get("started_at")), "ended_at": _aware(r.get("ended_at")),
                 "ok": None if r.get("ok") is None else bool(r.get("ok")), "note": r.get("note")}
                for r in run_log.recent(job["id"], 5)]
        out.append({"kind": "schedule", "id": f"job-{job['id']}", "runs": runs,
                    "title": job.get("name") or job.get("ticket_title") or f"job {job['id']}",
                    "state": "running" if running else "scheduled",
                    "cron": job.get("cron"), "next_run_at": _aware(job.get("next_run_at")),
                    "last_run_at": _aware(job.get("last_run_at")),
                    "expires_at": _aware(job.get("expires_at")),
                    "last_error": (job.get("last_error") or "")[:200] or None})
    return out


#: Finished side tasks shown for this long, at most this many.
_SIDE_RECENT_S, _SIDE_SHOWN = 6 * 3600, 6


def _side_items(session_id) -> list:
    """The chat's side tasks: queued and running ones, and the last few that
    ended (done or stopped) — several run at once, each its own row. Read
    straight from the store (no settle write, no transcript read per poll);
    a task marked running whose run is gone shows as stopped."""
    import time
    from aiforge_core.runtime import chat_runs, chat_store
    out, done = [], []
    for child in chat_store.child_sessions(int(session_id)):
        task = child.get("task") or {}
        state = str(task.get("state") or "queued")
        started, ended = _num(task.get("started")), _num(task.get("ended"))
        if state == "running" and not chat_runs.is_running(child["id"]) \
                and not (started and time.time() - started < 30):
            state = "stopped"          # the run is gone (a fresh start is let be)
            ended = ended or started or time.time()
        row = {"kind": "side_task", "id": f"side-{child['id']}", "chat_id": child["id"],
               "title": child.get("title") or str(task.get("prompt") or "")[:100]
               or f"side task {child['id']}",
               "state": state, "started_at": _epoch(started),
               "ended_at": _epoch(ended),
               "last_error": (str(task.get("error") or ""))[:200] or None}
        if state in ("running", "queued"):
            out.append(row)
        elif ended and time.time() - ended < _SIDE_RECENT_S:
            done.append((ended, row))
    done.sort(key=lambda e: e[0], reverse=True)
    return out + [r for _, r in done[:_SIDE_SHOWN]]


def _num(t) -> "float | None":
    """An epoch from the task record (an ISO string read as local time)."""
    if t in (None, ""):
        return None
    try:
        return float(t)
    except (TypeError, ValueError):
        try:
            return datetime.fromisoformat(str(t).replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None


def _epoch(t) -> "str | None":
    try:
        return datetime.fromtimestamp(float(t)).astimezone().isoformat() if t else None
    except (TypeError, ValueError, OSError):
        return _aware(t)


def items(session_id) -> list:
    """Every piece of background work of the chat, busy ones first."""
    if session_id is None:
        return []
    out: list = []
    for part in (_bg_items, _schedule_items, _side_items):
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


def stoppable(items: list) -> bool:
    """Something the background stop ends is running (a side task has its
    own stop, in its own chat)."""
    return any(i["state"] == "running" and i["kind"] != "side_task" for i in items)


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


__all__ = ["active", "active_sessions", "items", "stop", "stoppable"]
