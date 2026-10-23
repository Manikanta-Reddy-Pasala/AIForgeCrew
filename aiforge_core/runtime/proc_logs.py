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
import os
import threading
import time

from aiforge_core.runtime import cmd_signals as sig
from aiforge_core.runtime import proc_log_files

#: Bytes of each stream kept after a job is collected.
ENDED_KEEP_BYTES = 64 * 1024
#: Ended jobs kept per chat.
ENDED_PER_SESSION = 12
#: Most bytes one read returns per stream (the UI asks again for more).
READ_MAX_BYTES = 256 * 1024

_LOCK = threading.Lock()
_ENDED: dict[str, collections.deque] = {}
#: (chat, job key) → when the agent asked to show it. Wall-clock microseconds,
#: so the number keeps rising across a server restart (a tab left open
#: compares it with the last one it saw).
_SHOWN: dict[tuple[str, str], int] = {}
_LAST_SEQ = [0]
#: Chats deleted: a job of theirs that ends later keeps nothing here.
_FORGOTTEN: set[str] = set()


def _sid(session_id) -> str:
    return str(session_id)


def _shown(session_id, key: str) -> int:
    return _SHOWN.get((_sid(session_id), key), 0)


def _next_seq() -> int:
    # Microseconds: rises across a restart and stays exact in a JS number.
    _LAST_SEQ[0] = max(_LAST_SEQ[0] + 1, time.time_ns() // 1000)
    return _LAST_SEQ[0]


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
        "shown": _shown(job.session_id, job.key),
        "log_files": _log_files(job, alive),
    }


def _log_files(job, alive: bool) -> list[str]:
    try:
        # A job that ended still names its files on its command line.
        return proc_log_files.detect(job)
    except Exception:  # noqa: BLE001 — a listing never fails on this
        return []


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
        if job.session_id is None or _sid(job.session_id) in _FORGOTTEN \
                or job.closed:
            return
        status = _status(job)
        tails = []
        for s in job.streams:
            end = sig.file_size(s)
            start = max(0, end - ENDED_KEEP_BYTES)
            tails.append({"base": start,
                          "data": sig.read_range(s, start, end)})
        if job.closed:
            # Another thread collected and closed it while we read: what we
            # read is empty, and its own record is the good one.
            return
        status.update(running=False, ended_at=round(time.time(), 3),
                      _tails=tails,
                      _log_ids=dict(getattr(job, "log_ids", None) or {}))
        with _LOCK:
            q = _ENDED.setdefault(_sid(job.session_id),
                                  collections.deque(maxlen=ENDED_PER_SESSION))
            for old in [r for r in q if r["id"] == job.key]:
                q.remove(old)
            if len(q) == q.maxlen:              # the oldest drops out
                _SHOWN.pop((_sid(job.session_id), q[0]["id"]), None)
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
        ended = [{**{k: v for k, v in r.items() if not k.startswith("_")},
                  "shown": _shown(session_id, r["id"])}
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


#: A running command's unfinished last line is held back until it ends with
#: a newline — unless this much is pending, or the output has been quiet
#: this long (a prompt such as "Password: ", a progress bar waiting).
_PARTIAL_HOLD_BYTES = 4096
_PARTIAL_QUIET_S = 1.5


def _quiet_for(stream) -> float:
    try:
        mtime = (os.path.getmtime(stream) if isinstance(stream, str)
                 else os.fstat(stream.fileno()).st_mtime)
    except (OSError, ValueError):
        return 0.0
    return time.time() - mtime


def _whole_lines(raw: bytes, start: int, stream=None) -> tuple[bytes, int]:
    """``raw`` cut after its last newline, and where the next read starts.
    A line (or a UTF-8 character) split across two polls stays in one."""
    cut = raw.rfind(b"\n") + 1
    if cut == len(raw):
        return raw, start + len(raw)
    if len(raw) - cut >= _PARTIAL_HOLD_BYTES or (
            stream is not None and _quiet_for(stream) >= _PARTIAL_QUIET_S):
        return raw, start + len(raw)
    return raw[:cut], start + cut


def _read_stream(stream, offset: int, max_bytes: int, running: bool) -> dict:
    size = sig.file_size(stream)
    start = max(0, min(int(offset), size))
    if offset < 0:                          # negative: the last N bytes
        start = max(0, size + int(offset))
    skipped = 0
    if size - start > max_bytes:
        skipped = size - start - max_bytes
        start = size - max_bytes
    raw, end = sig.read_range(stream, start, size), size
    if running:
        raw, end = _whole_lines(raw, start, stream)
    out = _chunk(raw, start, end, size)
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
    if job is not None and not job.closed:
        status = _status(job)
        offs = list(offsets or [])
        streams = []
        for i, s in enumerate(job.streams):
            off = offs[i] if i < len(offs) else -max_bytes
            streams.append(_read_stream(s, off, max_bytes, status["running"]))
        status["streams"] = streams
        if not job.closed:
            return status
        # Collected while we read (a closed stream reads as empty): answer
        # from the kept tail instead, so the offsets never go backwards.
    rec = _find_ended(session_id, key)
    if rec is None:
        return None
    status = {k: v for k, v in rec.items() if not k.startswith("_")}
    status["shown"] = _shown(session_id, key)
    offs = list(offsets or [])
    status["streams"] = [
        _read_tail(t, offs[i] if i < len(offs) else -max_bytes, max_bytes)
        for i, t in enumerate(rec["_tails"])]
    return status


def _ids_of(session_id, key: str) -> tuple[dict, bool] | None:
    """The log files listed for job ``key`` of this chat (path → identity),
    and whether the job still runs. None when the chat has no such job."""
    job = _find_live(session_id, key)
    if job is not None:
        alive = job.alive()
        _log_files(job, alive)
        return dict(getattr(job, "log_ids", None) or {}), alive
    rec = _find_ended(session_id, key)
    if rec is None:
        return None
    return dict(rec.get("_log_ids") or {}), False


def open_file(session_id, key: str, path: str):
    """``(real path, open file, running)`` for a log file listed for that
    job and still the same file; None otherwise. The caller closes it."""
    got = _ids_of(session_id, key)
    if got is None:
        return None
    opened = proc_log_files.open_listed(got[0], path)
    return (opened[0], opened[1], got[1]) if opened else None


def read_file(session_id, key: str, path: str, offset: int = -64 * 1024,
              max_bytes: int = READ_MAX_BYTES) -> dict | None:
    """A log file of job ``key`` from ``offset`` on (negative: the last N
    bytes), whole lines while the job runs. None when ``path`` is not one of
    that job's log files."""
    opened = open_file(session_id, key, path)
    if opened is None:
        return None
    real, fh, running = opened
    max_bytes = max(1024, min(int(max_bytes or READ_MAX_BYTES),
                              READ_MAX_BYTES))
    try:
        out = _read_stream(fh, int(offset), max_bytes, running)
    finally:
        fh.close()
    out.update(path=real, running=running)
    return out


def file_chunks(fh, chunk: int = 256 * 1024):
    """A whole open file up to its size NOW (a log still being written does
    not grow the download past what was promised), then closed."""
    try:
        end = os.fstat(fh.fileno()).st_size
        pos = 0
        while pos < end:
            data = os.pread(fh.fileno(), min(chunk, end - pos), pos)
            if not data:
                yield b"\n... (the file got shorter while it was read) ...\n"
                break
            pos += len(data)
            yield data
    finally:
        fh.close()


def output_chunks(session_id, key: str, chunk: int = 256 * 1024):
    """The job's whole output for a download: stdout, then stderr (marked).
    An ended job has only its kept tail. None when the chat has no such job."""
    job = _find_live(session_id, key)
    if job is not None and not job.closed:
        streams = [(s, 0, sig.file_size(s)) for s in job.streams]
        def gen():
            for i, (s, start, end) in enumerate(streams):
                if i and end > start:
                    yield b"\n----- stderr -----\n"
                pos = start
                while pos < end:
                    data = sig.read_range(s, pos, min(end, pos + chunk))
                    if not data:
                        yield (b"\n... (output cut short: the command was "
                               b"collected while it was read) ...\n")
                        break
                    pos += len(data)
                    yield data
        return gen()
    rec = _find_ended(session_id, key)
    if rec is None:
        return None
    def gen_ended():
        for i, t in enumerate(rec["_tails"]):
            if i and t["data"]:
                yield b"\n----- stderr -----\n"
            if t["base"]:
                yield f"... (first {t['base']} bytes not kept) ...\n".encode()
            yield t["data"]
    return gen_ended()


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
        _SHOWN[(_sid(session_id), key)] = _next_seq()
        if len(_SHOWN) > 512:
            for old in sorted(_SHOWN, key=_SHOWN.get)[:256]:
                _SHOWN.pop(old, None)
    return True


def forget_session(session_id) -> None:
    """A chat was deleted: stop the log tails ``show_logs`` started for it
    (nothing else could reach them now) and drop what is kept for it. Its
    other commands keep running as before; their output is not kept."""
    sid = _sid(session_id)
    with _LOCK:
        _FORGOTTEN.add(sid)
    for job in _live_jobs(session_id):
        if getattr(job, "follow_path", None) and job.alive():
            job.kill("stopped: its chat was deleted")
    with _LOCK:
        _ENDED.pop(sid, None)
        for k in [k for k in _SHOWN if k[0] == sid]:
            _SHOWN.pop(k, None)


def _reset_for_tests() -> None:
    with _LOCK:
        _ENDED.clear()
        _SHOWN.clear()
        _FORGOTTEN.clear()


__all__ = ["ENDED_KEEP_BYTES", "ENDED_PER_SESSION", "READ_MAX_BYTES",
           "file_chunks", "forget_session", "kill", "list_for_session",
           "mark_shown", "open_file", "output_chunks", "read", "read_file",
           "remember"]
