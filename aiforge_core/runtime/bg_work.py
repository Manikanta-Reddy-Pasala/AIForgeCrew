"""Background commands and watches that do not hold a chat turn.

A watch or a command marked as background returns a handle at once. The
work runs on a daemon thread, so the eight producer slots are free and
another watch can start in the same session. When it finishes, one short
line is appended to that chat. Stop kills every background task in the
session.

Rows live in ``$AIFORGE_CONFIG_DIR/background.db``. A restart resumes a
watch that had not finished, once. A second restart marks it stopped.
A background command is not started again: if its process is still alive
the waiter is reattached, otherwise the chat is told it stopped.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime

log = logging.getLogger("aiforge.bg")

_LOCK = threading.Lock()
_EVENTS: dict[int, threading.Event] = {}
_SCHEMA_DONE: set[str] = set()

_DDL = """
CREATE TABLE IF NOT EXISTS bg (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER,
  kind TEXT NOT NULL,
  cwd TEXT,
  payload TEXT NOT NULL,
  status TEXT NOT NULL,
  attempt INTEGER NOT NULL DEFAULT 1,
  pid INTEGER,
  pgid INTEGER,
  created_at TEXT NOT NULL
);
"""


def _db_path() -> str:
    raw = os.environ.get("AIFORGE_BG_DB_PATH")
    if raw:
        return os.path.expanduser(raw)
    from aiforge_core.config.paths import config_dir
    return os.path.join(os.path.expanduser(str(config_dir())), "background.db")


def _connect():
    path = _db_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    con = sqlite3.connect(path, timeout=30.0)
    con.row_factory = sqlite3.Row
    if path not in _SCHEMA_DONE:
        con.executescript(_DDL)
        _SCHEMA_DONE.add(path)
    return con


def _row(r) -> dict:
    return dict(r) if r else {}


def _insert(session_id, kind: str, cwd: str, payload: dict, *,
            pid=None, pgid=None, attempt: int = 1) -> int:
    with _LOCK:
        con = _connect()
        try:
            cur = con.execute(
                "INSERT INTO bg (session_id, kind, cwd, payload, status, "
                "attempt, pid, pgid, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (session_id, kind, cwd, json.dumps(payload), "running",
                 attempt, pid, pgid,
                 datetime.now().isoformat(timespec="seconds")))
            con.commit()
            return int(cur.lastrowid)
        finally:
            con.close()


def _update(wid: int, **fields) -> None:
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values())
    with _LOCK:
        con = _connect()
        try:
            con.execute(f"UPDATE bg SET {sets} WHERE id=?", (*vals, wid))
            con.commit()
        finally:
            con.close()


def _get(wid: int) -> dict | None:
    con = _connect()
    try:
        r = con.execute("SELECT * FROM bg WHERE id=?", (wid,)).fetchone()
        return _row(r) if r else None
    finally:
        con.close()


def _running() -> list[dict]:
    con = _connect()
    try:
        rs = con.execute(
            "SELECT * FROM bg WHERE status='running' ORDER BY id").fetchall()
        return [_row(r) for r in rs]
    finally:
        con.close()


def _running_for(session_id) -> list[dict]:
    con = _connect()
    try:
        rs = con.execute(
            "SELECT * FROM bg WHERE status='running' AND session_id=? "
            "ORDER BY id", (session_id,)).fetchall()
        return [_row(r) for r in rs]
    finally:
        con.close()


def _post(session_id, text: str) -> None:
    if not session_id or not text:
        return
    line = " ".join(str(text).split())
    if len(line) > 400:
        line = line[:399] + "…"
    try:
        from aiforge_core.runtime import chat_store
        chat_store.add_message(int(session_id), "assistant", line)
    except Exception as exc:  # noqa: BLE001
        log.warning("bg post session=%s: %s", session_id, exc)
        return
    try:
        from aiforge_core.runtime import hooks
        fired = hooks.fire(
            "Notification",
            {"reason": "finished", "text": line}, None)
        note = hooks.context_note(fired)
        if note:
            from aiforge_core.runtime import chat_store
            chat_store.add_message(
                int(session_id), "user",
                f"[hook Notification — not the user]\n{note}")
    except Exception:  # noqa: BLE001
        pass


def _bind(wid: int) -> threading.Event:
    ev = threading.Event()
    with _LOCK:
        _EVENTS[wid] = ev
    return ev


def _unbind(wid: int) -> None:
    with _LOCK:
        _EVENTS.pop(wid, None)


def _alive(pid) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except OSError:
        return False
    else:
        return True


def _kill(pgid) -> None:
    if not pgid:
        return
    try:
        from aiforge_core.runtime import proc_signals
        proc_signals.stop_group(int(pgid), pause_s=0.0)
    except Exception:  # noqa: BLE001
        pass


def _stop_row(row: dict) -> None:
    ev = _EVENTS.get(row["id"])
    if ev is not None:
        ev.set()
    _kill(row.get("pgid"))
    # Persist the stop so a restart does not resume work the user ended.
    # The worker posts the one-line outcome when it notices.
    _update(row["id"], status="stopped")


def stop_all() -> int:
    """Stop every background command and watch, including ones with no chat."""
    try:
        rows = _running()
    except Exception:  # noqa: BLE001
        return 0
    for row in rows:
        _stop_row(row)
    return len(rows)


def stop_session(session_id) -> int:
    """Stop every background command and watch in this chat. Returns how many."""
    try:
        sid = int(session_id)
    except (TypeError, ValueError):
        return 0
    rows = _running_for(sid)
    for row in rows:
        _stop_row(row)
    return len(rows)


def _claim_attempt(wid: int, expected: int) -> bool:
    """One process wins this restart of a background row."""
    with _LOCK:
        con = _connect()
        try:
            cur = con.execute(
                "UPDATE bg SET attempt=? WHERE id=? AND status='running' "
                "AND attempt=?",
                (int(expected) + 1, wid, int(expected)))
            con.commit()
            return (cur.rowcount or 0) > 0
        finally:
            con.close()


def _stop_retried(wid: int) -> bool:
    with _LOCK:
        con = _connect()
        try:
            cur = con.execute(
                "UPDATE bg SET status='stopped' WHERE id=? "
                "AND status='running' AND attempt>=2",
                (wid,))
            con.commit()
            return (cur.rowcount or 0) > 0
        finally:
            con.close()


def _handle(wid: int, extra: dict) -> dict:
    out = {"ok": True, "background": True, "handle": f"bg-{wid}",
           "watch_id": wid}
    out.update(extra)
    return out


def start_watch(session_id: int, cwd: str, args: dict) -> dict:
    """Start a watch_until loop off the producer thread."""
    payload = {"args": {k: v for k, v in (args or {}).items()
                        if k not in ("inline", "background")}}
    wid = _insert(session_id, "watch", cwd, payload)
    _spawn(wid)
    return _handle(wid, {
        "note": "Watch is running in the background. Keep working or start "
                "another watch; the result will appear in this chat. Stop, "
                "or a message that drops or replaces the work, ends it. "
                "An extra detail does not."})


def start_gitlab(session_id: int, cwd: str, args: dict) -> dict:
    payload = {"args": {k: v for k, v in (args or {}).items()
                        if k != "inline"}}
    wid = _insert(session_id, "gitlab", cwd, payload)
    _spawn(wid)
    return _handle(wid, {
        "note": "Pipeline watch is running in the background and does not "
                "hold this turn. The result will appear in this chat."})


def track_command(session_id, cwd: str, cmd: str, proc, spool) -> dict:
    """Keep ``proc`` running after the tool returns. Stop can still kill it."""
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        pgid = proc.pid
    wid = _insert(session_id, "command", cwd, {"cmd": cmd},
                  pid=proc.pid, pgid=pgid)
    ev = _bind(wid)
    if session_id is not None:
        try:
            from aiforge_core.runtime import chat_cancel
            chat_cancel.track_pgid(int(session_id), pgid)
        except Exception:  # noqa: BLE001
            pass
    threading.Thread(
        target=_wait_command, name=f"bg-cmd-{wid}", daemon=True,
        args=(wid, proc, spool, ev, cmd, session_id, pgid)).start()
    return {"ok": True, "background": True, "pid": proc.pid, "pgid": pgid,
            "handle": f"bg-{wid}",
            "note": "Running in the background. This turn can continue. "
                    "The outcome will show up in this chat when it exits. "
                    "Stop kills it."}


def _wait_command(wid, proc, spool, ev, cmd, session_id, pgid) -> None:
    try:
        while proc.poll() is None:
            if ev.is_set():
                _kill(pgid)
                try:
                    proc.wait(timeout=3)
                except Exception:  # noqa: BLE001
                    pass
                break
            time.sleep(0.2)
        code = proc.returncode
        short = (cmd or "").strip().replace("\n", " ")[:80]
        if ev.is_set():
            text = f"Background command stopped: {short}"
        else:
            text = f"Background command finished (exit {code}): {short}"
            if code not in (0, None) and spool is not None:
                try:
                    _out, err = spool.read()
                    line = ((err or _out or "").strip().splitlines() or [""])[-1]
                    if line:
                        text += f" — {line[:120]}"
                except Exception:  # noqa: BLE001
                    pass
        _update(wid, status="stopped" if ev.is_set() else "done")
        _post(session_id, text)
    finally:
        _unbind(wid)
        if spool is not None:
            try:
                spool.close()
            except Exception:  # noqa: BLE001
                pass


def _spawn(wid: int) -> None:
    ev = _bind(wid)
    threading.Thread(target=_run_saved, name=f"bg-{wid}", daemon=True,
                     args=(wid, ev)).start()


def _run_saved(wid: int, ev: threading.Event) -> None:
    row = _get(wid)
    if not row or row.get("status") != "running":
        if row and row.get("status") == "stopped":
            _post(row.get("session_id"), "Watch stopped.")
        _unbind(wid)
        return
    from aiforge_core.runtime.run_interrupt import bind_stop_event
    from aiforge_core.runtime import chat_cancel
    sid = row.get("session_id")
    if sid is not None:
        try:
            chat_cancel.set_active(int(sid))
        except Exception:  # noqa: BLE001
            pass
    bind_stop_event(ev)
    try:
        payload = json.loads(row.get("payload") or "{}")
        args = payload.get("args") or {}
        cwd = row.get("cwd") or "."
        if row.get("kind") == "gitlab":
            from aiforge_core.runtime.tools import gitlab
            res = gitlab.gitlab_pipeline_watch(args, cwd)
        else:
            from aiforge_core.runtime.chat_agent._tools._watch import (
                _watch_until_blocking)
            res = _watch_until_blocking(args, cwd)
        _finish_watch(row, res if isinstance(res, dict) else {})
    except Exception as exc:  # noqa: BLE001
        log.warning("bg watch %s failed: %s", wid, exc)
        _update(wid, status="stopped")
        _post(sid, "Watch stopped: it could not keep running.")
    finally:
        bind_stop_event(None)
        _unbind(wid)


def _finish_watch(row: dict, res: dict) -> None:
    wid = row["id"]
    sid = row.get("session_id")
    if res.get("stopped") or res.get("steered"):
        _update(wid, status="stopped")
        why = "replaced" if res.get("steered") else "stopped"
        _post(sid, f"Watch {why}.")
        return
    _update(wid, status="done")
    if row.get("kind") == "gitlab":
        status = res.get("status") or ("passed" if res.get("passed") else "finished")
        _post(sid, f"Pipeline watch finished: {status}.")
        return
    if res.get("matched"):
        _post(sid, f"Watch finished: {res.get('reason') or 'condition met'} "
                   f"({res.get('checks')} checks).")
        return
    reason = res.get("reason") or res.get("error") or "ended"
    _post(sid, f"Watch ended: {str(reason)[:180]}")


def _reattach_command(row: dict) -> None:
    ev = _bind(row["id"])
    threading.Thread(
        target=_poll_pid, name=f"bg-reattach-{row['id']}", daemon=True,
        args=(row, ev)).start()


def _poll_pid(row: dict, ev: threading.Event) -> None:
    pid = row.get("pid")
    sid = row.get("session_id")
    try:
        payload = json.loads(row.get("payload") or "{}")
        short = str(payload.get("cmd") or "command").strip().replace("\n", " ")[:80]
    except Exception:  # noqa: BLE001
        short = "command"
    try:
        while _alive(pid):
            if ev.is_set():
                _kill(row.get("pgid") or pid)
                _update(row["id"], status="stopped")
                _post(sid, f"Background command stopped: {short}")
                return
            time.sleep(0.5)
        _update(row["id"], status="done")
        _post(sid, f"Background command ended: {short}")
    finally:
        _unbind(row["id"])


def resume_after_restart() -> int:
    """Restart unfinished watches once. Reattach a live command, or say it
    stopped. Never loops."""
    n = 0
    try:
        rows = _running()
    except Exception as exc:  # noqa: BLE001
        log.warning("bg resume query failed: %s", exc)
        return 0
    for row in rows:
        kind = row.get("kind")
        sid = row.get("session_id")
        if kind == "command":
            expected = int(row.get("attempt") or 1)
            if not _claim_attempt(row["id"], expected):
                continue
            if _alive(row.get("pid")):
                _reattach_command(row)
                n += 1
            else:
                _update(row["id"], status="stopped")
                _post(sid, "Background command stopped: it was not running "
                           "after restart.")
            continue
        attempt = int(row.get("attempt") or 1)
        if attempt >= 2:
            if _stop_retried(row["id"]):
                _post(sid, "Watch stopped: the process restarted and it was "
                           "already retried.")
            continue
        try:
            if not _claim_attempt(row["id"], attempt):
                continue
            _spawn(row["id"])
            n += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("bg resume %s: %s", row.get("id"), exc)
            _update(row["id"], status="stopped")
            _post(sid, "Watch stopped: it could not be resumed after restart.")
    return n
