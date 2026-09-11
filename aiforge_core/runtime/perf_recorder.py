"""Lightweight per-step perf recorder.

Appends one JSON line per timed event to ``$AIFORGE_CONFIG_DIR/perf.ndjson``
(default ``~/.aiforge/perf.ndjson``) and aggregates it for the /api/runtime/perf
endpoint consumed by the web Perf view.

Design rules:
  * **Soft-fail everywhere.** Perf instrumentation must NEVER raise into a
    running agent — every public function swallows its own exceptions.
  * **Cheap.** One short locked append per timed boundary; no background
    thread.
  * **Bounded.** Samples older than 7 days are dropped, and past a size cap
    only the newest are kept, so a long-lived host never grows the file
    without limit.

The ``family`` string is written verbatim into the ``event`` field that the
Perf page groups on. The page's ``familyOf(event)`` recognises the family
labels emitted here ("LLM", "Tool", "Queue", "Search", "File", "Edit cycle").
"""

from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager

try:
    import fcntl
except ImportError:          # native Windows: the thread lock alone
    fcntl = None

from aiforge_core.config import _atomic
from aiforge_core.config.paths import config_dir

# Samples older than this are dropped when the file is trimmed. The old rule
# (keep the last 5,000 lines once past 5 MB) meant a busy hour erased a week,
# and a quiet host kept month-old samples in every total.
_RETENTION_S = 7 * 86400
# Past this size a trim also keeps only the newest _TRIM_KEEP lines, so the file
# stays bounded even inside the retention window.
_MAX_BYTES = 5 * 1024 * 1024
_TRIM_KEEP = 20000
# The aggregate window the Perf page shows by default.
DEFAULT_WINDOW_S = 86400

# One lock for append, trim and reset. The thread lock orders this process; the
# flock on a sidecar file orders the others (API server, runner, CLI all write
# here). Without it a trim in process A read the file, process B appended, and
# A's atomic replace published a copy without B's line — silently lost.
_LOCK = threading.Lock()
# Stat + trim on the first record of a process and every Nth after it.
_TRIM_CHECK_EVERY = 64
_record_count = 0


def _config_dir() -> str:
    return str(config_dir())


def _perf_path() -> str:
    return os.path.join(_config_dir(), "perf.ndjson")


@contextmanager
def _locked(path: str):
    with _LOCK:
        if fcntl is None:
            yield
            return
        fh = open(path + ".lock", "a+")  # noqa: SIM115 — held for the block
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


def record(family: str, name: str, ms: float) -> None:
    """Append one perf sample. Soft-fail: never raises."""
    global _record_count
    try:
        path = _perf_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        line = json.dumps({
            "family": str(family),
            "name": str(name),
            "ms": float(ms),
            "ts": time.time(),
        })
        with _locked(path):
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        check = _record_count % _TRIM_CHECK_EVERY == 0
        _record_count += 1
        if check:
            _maybe_trim(path)
    except Exception:
        # Perf must never break a run.
        pass


def _oldest_ts(path: str) -> "float | None":
    try:
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if raw:
                    return float(json.loads(raw).get("ts") or 0.0)
    except Exception:
        return None
    return None


def _maybe_trim(path: str) -> None:
    """Drop samples past the retention window, and past the size cap keep only
    the newest _TRIM_KEEP. Runs under the shared lock and publishes through
    ``_atomic.write_text``, so readers see the whole old file or the whole new
    one and no concurrent append is lost."""
    try:
        with _locked(path):
            cutoff = time.time() - _RETENTION_S
            oversize = os.path.getsize(path) > _MAX_BYTES
            oldest = _oldest_ts(path)
            if not oversize and (oldest is None or oldest >= cutoff):
                return
            keep = []
            with open(path, encoding="utf-8") as fh:
                for raw in fh:
                    try:
                        if float(json.loads(raw).get("ts") or 0.0) >= cutoff:
                            keep.append(raw)
                    except Exception:
                        continue
            if oversize:
                keep = keep[-_TRIM_KEEP:]
            _atomic.write_text(path, "".join(keep))
    except Exception:
        pass


def _p95(values: list) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, int(round(0.95 * (len(s) - 1))))] if s else 0.0


def snapshot(window_s: "float | None" = DEFAULT_WINDOW_S) -> dict:
    """Group the samples of the last ``window_s`` seconds (all of them when
    ``window_s`` is 0/None) by (family, name).

    Rows are ``{event, name, count, total_ms, avg_ms, p95_ms, max_ms}``, sorted
    by total_ms desc. ``total_ms`` is SUMMED latency — parallel calls overlap,
    so it is not wall-clock. Also returns ``samples`` (the number of timed
    calls in the window) and ``oldest_ts`` (the first one kept). Soft-fails to
    an empty snapshot."""
    out = {"rows": [], "window_s": window_s or 0, "samples": 0, "oldest_ts": None}
    try:
        path = _perf_path()
        if not os.path.exists(path):
            return out
        since = time.time() - window_s if window_s else None
        buckets: dict = {}
        oldest = None
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                    ms = float(rec.get("ms", 0.0))
                    ts = float(rec.get("ts") or 0.0)
                except Exception:
                    continue
                if since is not None and ts < since:
                    continue
                oldest = ts if oldest is None else min(oldest, ts)
                key = (str(rec.get("family", "Other")), str(rec.get("name", "?")))
                buckets.setdefault(key, []).append(ms)
        rows = []
        for (family, name), vals in buckets.items():
            total = sum(vals)
            rows.append({"event": family, "name": name, "count": len(vals),
                         "total_ms": total, "avg_ms": total / len(vals),
                         "p95_ms": _p95(vals), "max_ms": max(vals)})
        rows.sort(key=lambda r: r["total_ms"], reverse=True)
        out.update(rows=rows, samples=sum(r["count"] for r in rows),
                   oldest_ts=oldest)
        return out
    except Exception:
        return out


def aggregate(window_s: "float | None" = None) -> list[dict]:
    """The rows of :func:`snapshot` — every sample kept unless ``window_s``."""
    return snapshot(window_s)["rows"]


def reset() -> None:
    """Truncate the ndjson file. Soft-fail: never raises."""
    try:
        path = _perf_path()
        if os.path.exists(path):
            with _locked(path):
                open(path, "w", encoding="utf-8").close()
    except Exception:
        pass


@contextmanager
def timed(family: str, name: str):
    """Context manager: record elapsed ms for the enclosed block on exit.

    Records on both normal exit and exceptions so a failing tool/LLM call is
    still accounted for. Soft-fails — instrumentation never masks the body's
    own exception, and a recorder fault is swallowed.
    """
    t0 = time.perf_counter()
    try:
        yield
    finally:
        try:
            record(family, name, (time.perf_counter() - t0) * 1000.0)
        except Exception:
            pass
