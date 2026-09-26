"""Background COMMANDS (the process side of :mod:`aiforge_core.runtime.bg_work`).

A command that keeps running after its tool returned — an explicit
background command, or a foreground one handed back to the model at a
check-in — is watched here on a daemon thread. The thread applies the
last-resort guards even while no turn is waiting on it: a wall-clock
deadline, no output and no CPU for too long, and the output-size cap
(``AIFORGE_CHAT_CMD_OUTPUT_MAX_MB``). A tripped guard kills the process group
and is remembered, so the next look at the job says WHY it ended.

The rows, the Stop wiring and the chat line live in ``bg_work``; its names
are looked up there at call time, so they stay patchable in one place.
"""
from __future__ import annotations

import json
import os
import threading
import time

_TRIPPED: dict[int, str] = {}
_TRIP_LOCK = threading.Lock()
_MAX_TRIPPED = 256


def _bg():
    from aiforge_core.runtime import bg_work
    return bg_work


def track_command(session_id, cwd: str, cmd: str, proc, spool, *,
                  close_spool: bool = True, announce: bool = True,
                  idle_s: float = 0.0, deadline: float | None = None,
                  owner: str | None = None) -> dict:
    """Keep ``proc`` running after the tool returns. Stop can still kill it.

    ``close_spool=False`` leaves the output files to their other owner (the
    agent's job table, which still reads them). ``announce=False`` skips the
    end-of-run chat line — for a command the agent itself is watching.
    ``idle_s`` / ``deadline`` carry a handed-off foreground command's
    last-resort guards: killed after that long with no output and no CPU, or
    at that ``time.monotonic()`` deadline. The output-size cap applies to
    every spooled command."""
    bg = _bg()
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        pgid = proc.pid
    payload = {"cmd": cmd, **({"owner": owner} if owner else {})}
    wid = bg._insert(session_id, "command", cwd, payload,
                     pid=proc.pid, pgid=pgid)
    ev = bg._bind(wid)
    if session_id is not None:
        try:
            from aiforge_core.runtime import chat_cancel
            chat_cancel.track_pgid(int(session_id), pgid)
        except Exception:  # noqa: BLE001
            pass
    opts = {"close_spool": close_spool, "announce": announce,
            "idle_s": idle_s, "deadline": deadline}
    threading.Thread(
        target=_wait_command, name=f"bg-cmd-{wid}", daemon=True,
        args=(wid, proc, spool, ev, cmd, session_id, pgid, opts)).start()
    return {"ok": True, "background": True, "pid": proc.pid, "pgid": pgid,
            "handle": f"bg-{wid}", "opts": opts,
            "note": "Running in the background. This turn can continue. "
                    "The outcome will show up in this chat when it exits. "
                    "Stop kills it."}


def stop_command(wid) -> bool:
    """Ask the watcher of background command ``wid`` to stop it (it kills the
    group and marks the row stopped). False when nothing watches it."""
    try:
        ev = _bg()._EVENTS.get(int(wid))
    except (TypeError, ValueError):
        return False
    if ev is None:
        return False
    ev.set()
    return True


def trip_reason(wid) -> str | None:
    """Why the watcher killed command ``wid`` (a guard), or None."""
    try:
        key = int(wid)
    except (TypeError, ValueError):
        return None
    with _TRIP_LOCK:
        return _TRIPPED.get(key)


def _note_trip(wid: int, why: str) -> None:
    with _TRIP_LOCK:
        _TRIPPED[wid] = why
        while len(_TRIPPED) > _MAX_TRIPPED:
            _TRIPPED.pop(next(iter(_TRIPPED)))


def _guard_tripped(proc, clock, deadline, spool=None) -> str | None:
    """Which last-resort guard tripped, as a message for the model, or None."""
    if deadline is not None and time.monotonic() > deadline:
        return ("timed out at its wall clock — PARTIAL output. Run a narrower "
                "command or give it a larger \"timeout\".")
    if clock is not None and clock.stalled():
        return (f"stopped: no output and no CPU activity for "
                f"{int(clock.idle_s)}s — the command looks HUNG.")
    too_big = getattr(spool, "too_big", None) if spool is not None else None
    if callable(too_big):
        try:
            if too_big():
                return spool.too_big_error()
        except Exception:  # noqa: BLE001 — a closed spool is not too big
            return None
    return None


def _wait_command(wid, proc, spool, ev, cmd, session_id, pgid,
                  opts: dict | None = None) -> None:
    bg = _bg()
    opts = opts or {}
    clock = None
    if opts.get("idle_s") and spool is not None:
        from aiforge_core.runtime.cmd_idle import ProgressClock
        clock = ProgressClock(pgid, spool.size, float(opts["idle_s"]))
    tripped = None
    try:
        while proc.poll() is None:
            tripped = None if ev.is_set() else _guard_tripped(
                proc, clock, opts.get("deadline"), spool)
            if tripped:
                _note_trip(wid, tripped)
            if ev.is_set() or tripped:
                bg._kill(pgid)
                try:
                    proc.wait(timeout=3)
                except Exception:  # noqa: BLE001
                    pass
                break
            time.sleep(0.2)
        code = proc.returncode
        short = (cmd or "").strip().replace("\n", " ")[:80]
        stopped = ev.is_set() or bool(tripped)
        if ev.is_set():
            text = f"Background command stopped: {short}"
        elif tripped:
            text = f"Background command stopped: {short} — {tripped[:160]}"
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
        bg._update(wid, status="stopped" if stopped else "done")
        if opts.get("announce", True):
            bg._post(session_id, text)
    finally:
        bg._unbind(wid)
        if spool is not None and opts.get("close_spool", True):
            try:
                spool.close()
            except Exception:  # noqa: BLE001
                pass


def _reattach_command(row: dict) -> None:
    ev = _bg()._bind(row["id"])
    threading.Thread(
        target=_poll_pid, name=f"bg-reattach-{row['id']}", daemon=True,
        args=(row, ev)).start()


def _poll_pid(row: dict, ev: threading.Event) -> None:
    bg = _bg()
    pid = row.get("pid")
    sid = row.get("session_id")
    try:
        payload = json.loads(row.get("payload") or "{}")
        short = str(payload.get("cmd") or "command").strip().replace("\n", " ")[:80]
    except Exception:  # noqa: BLE001
        short = "command"
    try:
        while bg._alive(pid):
            if ev.is_set():
                bg._kill(row.get("pgid") or pid)
                bg._update(row["id"], status="stopped")
                bg._post(sid, f"Background command stopped: {short}")
                return
            time.sleep(0.5)
        bg._update(row["id"], status="done")
        bg._post(sid, f"Background command ended: {short}")
    finally:
        bg._unbind(row["id"])


__all__ = ["stop_command", "track_command", "trip_reason"]
