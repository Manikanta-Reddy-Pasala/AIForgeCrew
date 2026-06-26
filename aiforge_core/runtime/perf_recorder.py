"""Lightweight per-step perf recorder.

Appends one JSON line per timed event to ``$AIFORGE_CONFIG_DIR/perf.ndjson``
(default ``~/.aiforge/perf.ndjson``) and aggregates it for the /api/runtime/perf
endpoint consumed by the web Perf view.

Design rules:
  * **Soft-fail everywhere.** Perf instrumentation must NEVER raise into a
    running agent — every public function swallows its own exceptions.
  * **Cheap.** One append per timed boundary; no locks, no background thread.
  * **Bounded.** The ndjson is trimmed to the last N lines once it grows past
    a soft size cap so a long-lived host never accumulates an unbounded file.

The ``family`` string is written verbatim into the ``event`` field that the
Perf page groups on. The page's ``familyOf(event)`` recognises the family
labels emitted here ("LLM", "Tool", "Search", "File", "Edit cycle").
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager

# Soft size cap (~5 MB). When exceeded we keep only the last _TRIM_KEEP lines.
_MAX_BYTES = 5 * 1024 * 1024
_TRIM_KEEP = 5000


def _config_dir() -> str:
    return os.path.expanduser(os.environ.get("AIFORGE_CONFIG_DIR", "~/.aiforge"))


def _perf_path() -> str:
    return os.path.join(_config_dir(), "perf.ndjson")


def record(family: str, name: str, ms: float) -> None:
    """Append one perf sample. Soft-fail: never raises."""
    try:
        path = _perf_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        line = json.dumps({
            "family": str(family),
            "name": str(name),
            "ms": float(ms),
            "ts": time.time(),
        })
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        _maybe_trim(path)
    except Exception:
        # Perf must never break a run.
        pass


def _maybe_trim(path: str) -> None:
    """Trim the ndjson to the last _TRIM_KEEP lines if it grew past the cap."""
    try:
        if os.path.getsize(path) <= _MAX_BYTES:
            return
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
        keep = lines[-_TRIM_KEEP:]
        with open(path, "w", encoding="utf-8") as fh:
            fh.writelines(keep)
    except Exception:
        pass


def aggregate() -> list[dict]:
    """Group samples by (family, name); return rows sorted by total_ms desc.

    Row shape matches the Perf page: ``{event, name, count, total_ms, max_ms}``.
    Soft-fail to ``[]`` on any error.
    """
    try:
        path = _perf_path()
        if not os.path.exists(path):
            return []
        buckets: dict[tuple[str, str], dict] = {}
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except Exception:
                    continue
                family = str(rec.get("family", "Other"))
                name = str(rec.get("name", "?"))
                try:
                    ms = float(rec.get("ms", 0.0))
                except Exception:
                    ms = 0.0
                key = (family, name)
                b = buckets.get(key)
                if b is None:
                    buckets[key] = {
                        "event": family,
                        "name": name,
                        "count": 1,
                        "total_ms": ms,
                        "max_ms": ms,
                    }
                else:
                    b["count"] += 1
                    b["total_ms"] += ms
                    if ms > b["max_ms"]:
                        b["max_ms"] = ms
        rows = list(buckets.values())
        rows.sort(key=lambda r: r["total_ms"], reverse=True)
        return rows
    except Exception:
        return []


def reset() -> None:
    """Truncate the ndjson file. Soft-fail: never raises."""
    try:
        path = _perf_path()
        if os.path.exists(path):
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
