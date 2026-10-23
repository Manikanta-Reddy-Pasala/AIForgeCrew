"""What the user sees of a chat's running commands: the Processes dock.

Every command the agent leaves running — a background ``run_command``, a
foreground one handed back at its check-in, a ``serve`` service, a log tail
started with ``show_logs`` — is a :mod:`aiforge_core.runtime.cmd_jobs` job
whose output sits in spool files. This module reads those for the web UI:

* :func:`list_for_session` — the chat's jobs, running ones first, plus the
  last few that ended (their output tail is kept when the job is collected,
  so the dock does not go blank the moment a command exits);
* :func:`read` — output from a byte offset on, per stream, so the UI tails a
  job by asking only for what is new;
* :func:`mark_shown` — the agent asked to put a job's log in front of the
  user (``show_logs``); the UI opens a tile for it.

Reads use ``os.pread`` (via ``cmd_signals.read_range``) and never move the
agent's own look position, so watching a log here does not change what
``command_output`` reports to the model.
"""
from __future__ import annotations

import collections
import itertools
import threading
import time

from aiforge_core.runtime import cmd_signals as sig

#: Bytes of each stream kept after a job is collected.
ENDED_KEEP_BYTES = 64 * 1024
#: Ended jobs kept per chat.
ENDED_PER_SESSION = 12
#: Most bytes one read returns per stream (the UI asks again for more).
READ_MAX_BYTES = 256 * 1024

_LOCK = threading.Lock()
_ENDED: dict[str, collections.deque] = {}
_SHOWN: dict[str, int] = {}          # job key → show sequence number
_SEQ = itertools.count(1)


def _sid(session_id) -> str:
    return str(session_id)


def _started_at(job) -> float:
    """Wall-clock start of a job (it records a monotonic one)."""
    return time.time() - (time.monotonic() - float(job.started))


def _status(job) -> dict:
    alive = job.alive()
    code = None if alive else getattr(job.proc, "returncode", None)
    return {
        "id": job.key,
        "cmd": " ".join(str(job.cmd).split())[:400],
        "label": str(getattr(job, "label", "") or "")[:120],
        "cwd": str(getattr(job, "cwd", "") or ""),
        "started_at": round(_started_at(job), 3),
        "running": alive,
        "code": code,
        "stopped": job.killed,
        "background": bool(job.explicit),
        "bytes": [sig.file_size(s) for s in job.streams],
        "shown": _SHOWN.get(job.key, 0),
    }


def _live_jobs(session_id) -> list:
    from aiforge_core.runtime import cmd_jobs
    want = _sid(session_id)
    with cmd_jobs._LOCK:
        return [j for j in cmd_jobs._JOBS.values()
                if j.session_id is not None and str(j.session_id) == want]


def _find_live(session_id, key: str):
    for job in _live_jobs(session_id):
        if job.key == key:
            return job
    return None


def _find_ended(session_id, key: str) -> dict | None:
    with _LOCK:
        for rec in _ENDED.get(_sid(session_id), ()):
            if rec["id"] == key:
                return rec
    return None


def remember(job) -> None:
    """Keep the output tail of a job that is being collected. Called by
    ``cmd_jobs._forget`` while the streams are still open; never raises."""
    try:
        if job.session_id is None:
            return
        status = _status(job)
        tails = []
        for s in job.streams:
            end = sig.file_size(s)
            start = max(0, end - ENDED_KEEP_BYTES)
            tails.append({"base": start,
                          "data": sig.read_range(s, start, end)})
        status.update(running=False, ended_at=round(time.time(), 3),
                      _tails=tails)
        with _LOCK:
            q = _ENDED.setdefault(_sid(job.session_id),
                                  collections.deque(maxlen=ENDED_PER_SESSION))
            for old in [r for r in q if r["id"] == job.key]:
                q.remove(old)
            q.append(status)
    except Exception:  # noqa: BLE001 — collecting a job must never fail here
        pass


def list_for_session(session_id) -> list[dict]:
    """The chat's jobs for the dock: running first (oldest first), then the
    ones that ended (newest first)."""
    live = []
    for job in _live_jobs(session_id):
        try:
            live.append(_status(job))
        except Exception:  # noqa: BLE001
            continue
    live_ids = {j["id"] for j in live}
    with _LOCK:
        ended = [{k: v for k, v in r.items() if k != "_tails"}
                 for r in _ENDED.get(_sid(session_id), ())
                 if r["id"] not in live_ids]
    running = sorted((j for j in live if j["running"]),
                     key=lambda j: j["started_at"])
    done = [j for j in live if not j["running"]] + list(reversed(ended))
    done.sort(key=lambda j: j.get("ended_at") or j["started_at"], reverse=True)
    return running + done


def _chunk(raw: bytes, start: int, end: int, size: int) -> dict:
    return {"text": sig.clean(raw), "start": start, "next": end,
            "size": size, "skipped": 0}


def _read_stream(stream, offset: int, max_bytes: int) -> dict:
    size = sig.file_size(stream)
    start = max(0, min(int(offset), size))
    if offset < 0:                          # negative: the last N bytes
        start = max(0, size + int(offset))
    skipped = 0
    if size - start > max_bytes:
        skipped = size - start - max_bytes
        start = size - max_bytes
    out = _chunk(sig.read_range(stream, start, size), start, size, size)
    out["skipped"] = skipped
    return out


def _read_tail(tail: dict, offset: int, max_bytes: int) -> dict:
    base, data = int(tail["base"]), tail["data"]
    size = base + len(data)
    start = size + int(offset) if offset < 0 else int(offset)
    # Bytes before ``base`` were not kept when the job was collected.
    skipped = base - start if 0 <= start < base else 0
    start = max(base, min(start, size))
    if size - start > max_bytes:
        skipped += size - start - max_bytes
        start = size - max_bytes
    out = _chunk(data[start - base:], start, size, size)
    out["skipped"] = skipped
    return out


def read(session_id, key: str, offsets: list[int] | None = None,
         max_bytes: int = READ_MAX_BYTES) -> dict | None:
    """Output of job ``key`` from ``offsets`` (one per stream: stdout,
    stderr) on. A negative offset means "the last N bytes". None when the
    chat has no such job."""
    max_bytes = max(1024, min(int(max_bytes or READ_MAX_BYTES),
                              READ_MAX_BYTES))
    job = _find_live(session_id, key)
    if job is not None:
        status = _status(job)
        offs = list(offsets or [])
        streams = []
        for i, s in enumerate(job.streams):
            off = offs[i] if i < len(offs) else -max_bytes
            streams.append(_read_stream(s, off, max_bytes))
        status["streams"] = streams
        return status
    rec = _find_ended(session_id, key)
    if rec is None:
        return None
    status = {k: v for k, v in rec.items() if k != "_tails"}
    status["shown"] = _SHOWN.get(key, status.get("shown", 0))
    offs = list(offsets or [])
    status["streams"] = [
        _read_tail(t, offs[i] if i < len(offs) else -max_bytes, max_bytes)
        for i, t in enumerate(rec["_tails"])]
    return status


def kill(session_id, key: str) -> bool:
    """Stop a running job of this chat (the dock's Stop). False when there is
    no such running job."""
    job = _find_live(session_id, key)
    if job is None or not job.alive():
        return False
    job.kill("stopped: the user stopped it from the Processes panel")
    return True


def mark_shown(session_id, key: str) -> bool:
    """Ask the UI to open job ``key``'s log. False when the chat has no such
    job."""
    if _find_live(session_id, key) is None \
            and _find_ended(session_id, key) is None:
        return False
    with _LOCK:
        _SHOWN[key] = next(_SEQ)
        if len(_SHOWN) > 512:
            for old in sorted(_SHOWN, key=_SHOWN.get)[:256]:
                _SHOWN.pop(old, None)
    return True


def forget_session(session_id) -> None:
    """Drop what is kept for a deleted chat."""
    with _LOCK:
        _ENDED.pop(_sid(session_id), None)


def _reset_for_tests() -> None:
    with _LOCK:
        _ENDED.clear()
        _SHOWN.clear()


__all__ = ["ENDED_KEEP_BYTES", "ENDED_PER_SESSION", "READ_MAX_BYTES",
           "forget_session", "kill", "list_for_session", "mark_shown",
           "read", "remember"]
