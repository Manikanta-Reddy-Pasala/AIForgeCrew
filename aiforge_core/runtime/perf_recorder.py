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
import threading
import time
from contextlib import contextmanager

from aiforge_core.config import _atomic
from aiforge_core.config.paths import config_dir

# Soft size cap (~5 MB). When exceeded we keep only the last _TRIM_KEEP lines.
_MAX_BYTES = 5 * 1024 * 1024
_TRIM_KEEP = 5000

# Serializes the read-modify-write of _maybe_trim and reset() so concurrent
# callers can't corrupt or lose samples (CC2). record()'s append stays
# lock-free (O_APPEND is atomic per line).
_TRIM_LOCK = threading.Lock()
# Only stat+trim every Nth record so the over-cap check (and its lock) isn't
# taken on the hot path of every single append — shrinks the racy window.
_TRIM_CHECK_EVERY = 64
_record_count = 0


def _config_dir() -> str:
    return str(config_dir())


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
        # Only check size every Nth record (a plain counter — an exact value
        # doesn't matter, it just throttles how often we stat + lock).
        global _record_count
        _record_count += 1
        if _record_count % _TRIM_CHECK_EVERY == 0:
            _maybe_trim(path)
    except Exception:
        # Perf must never break a run.
        pass


def _maybe_trim(path: str) -> None:
    """Trim the ndjson to the last _TRIM_KEEP lines if it grew past the cap.

    Serialized under _TRIM_LOCK and published through ``_atomic.write_text`` so
    a concurrent trim/reset can't interleave a truncate-in-place and lose or
    corrupt samples (CC2). Readers see either the whole old file or the whole
    new one, never a torn write. The lock only orders *this* process — a second
    process trimming the same file is covered by the helper's per-writer
    staging name, not by the lock."""
    try:
        with _TRIM_LOCK:
            if os.path.getsize(path) <= _MAX_BYTES:
                return
            with open(path, encoding="utf-8") as fh:
                lines = fh.readlines()
            _atomic.write_text(path, "".join(lines[-_TRIM_KEEP:]))
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
    """Truncate the ndjson file. Soft-fail: never raises.

    Serialized under the same lock as _maybe_trim (CC2) so a reset can't race a
    concurrent trim's read-modify-write."""
    try:
        path = _perf_path()
        with _TRIM_LOCK:
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
