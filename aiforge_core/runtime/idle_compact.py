"""Compact a chat's carried context once it has sat idle.

A chat left alone for a while (``idle_compact_min``, default 20 minutes) has
lost its prompt cache on the model server anyway: the next message pays to
re-read the whole carried transcript. Compacting it while nobody waits —
the same thing the Compact button does with no turn running
(:func:`chat_compact.compact_saved`) — makes that next message start lean.

* Only when no chat run is in flight anywhere (the model is shared; this must
  never slow down live work), and none started or ended in the last
  ``_SETTLE_S``.
* Only a transcript worth it: at least ``AIFORGE_IDLE_COMPACT_MIN_TOKENS``
  (default 12,000) estimated tokens.
* One try per saved transcript: a chat compacted (or found already short) is
  not looked at again until a new turn saves a new transcript.
* One chat per pass, so a long pass never holds the model when a user comes
  back. A compaction that has started is not cancelled by a new message: the
  message's turn runs alongside it, and the compacted transcript is only
  written when no newer one was saved meanwhile (``replace_work(expect=)``). ``idle_compact_min`` = 0 turns it off; ``AIFORGE_IDLE_COMPACT=0``
  keeps the schedule off (the test suite does).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time

log = logging.getLogger("aiforge.idle_compact")

#: Seconds between passes.
_EVERY_S = 300
#: Quiet the whole server must have had before a pass compacts anything.
_SETTLE_S = 120
#: Characters per token when the transcript did not record its own ratio.
_DEFAULT_CPT = 4.0

_RUN = threading.Lock()
#: session id → the transcript stamp already tried.
_TRIED: dict[str, tuple] = {}


def idle_minutes() -> int:
    try:
        from aiforge_core.config import runtime_settings
        return max(0, int(runtime_settings.get("idle_compact_min")))
    except Exception:  # noqa: BLE001
        return 20


def _min_tokens() -> int:
    try:
        return max(0, int(os.environ.get("AIFORGE_IDLE_COMPACT_MIN_TOKENS", "12000")))
    except ValueError:
        return 12000


def _tokens(path) -> "int | None":
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    msgs = data.get("messages") if isinstance(data, dict) else None
    if not isinstance(msgs, list):
        return None
    chars = 0
    for m in msgs:
        c = m.get("content") if isinstance(m, dict) else ""
        chars += len(c) if isinstance(c, str) else len(str(c))
    try:
        cpt = float(data.get("cpt") or _DEFAULT_CPT)
    except (TypeError, ValueError):
        cpt = _DEFAULT_CPT
    return int(chars / max(cpt, 1.0))


def _candidates(idle_s: float) -> list:
    """(session id, path) of transcripts idle long enough, oldest first."""
    from aiforge_core.runtime import chat_transcript
    try:
        files = list(chat_transcript._dir().glob("session_*.json"))
    except OSError:
        return []
    now = time.time()
    out = []
    for f in files:
        try:
            sid = int(f.stem.split("_", 1)[1])
            mtime = f.stat().st_mtime
        except (ValueError, IndexError, OSError):
            continue
        if now - mtime >= idle_s:
            out.append((mtime, sid, f))
    out.sort()
    return [(sid, f) for _m, sid, f in out]


def _busy() -> bool:
    from aiforge_core.runtime import chat_runs
    return chat_runs.any_active() or (
        time.time() - chat_runs.last_activity() < _SETTLE_S)


def sweep() -> dict:
    """One pass: compact at most one idle chat. Returns what it did."""
    minutes = idle_minutes()
    if minutes <= 0 or not _RUN.acquire(blocking=False):
        return {}
    try:
        return _sweep(minutes * 60)
    finally:
        _RUN.release()


def _sweep(idle_s: float) -> dict:
    from aiforge_core.runtime import (chat_compact, chat_runs, chat_store,
                                      chat_transcript)
    found = _candidates(idle_s)
    # A deleted chat (no transcript any more) is not remembered forever.
    if len(_TRIED) > 256:
        alive = {str(sid) for sid, _p in _candidates(0)}
        for k in [k for k in _TRIED if k not in alive]:
            _TRIED.pop(k, None)
    for sid, path in found:
        if _busy():
            return {"skipped": "busy"}
        stamp = chat_transcript.stamp(sid)
        if stamp is None or _TRIED.get(str(sid)) == stamp:
            continue
        sess = chat_store.get_session(sid)
        if not sess or chat_runs.start_lock_held(sid):
            continue                    # deleted, or a message is starting now
        tokens = _tokens(path)
        if tokens is None or tokens < _min_tokens():
            _TRIED[str(sid)] = stamp
            continue
        _TRIED[str(sid)] = stamp
        try:
            got = chat_compact.compact_saved(sid, role=sess.get("role") or "chat")
        except Exception as exc:  # noqa: BLE001 — try the next pass, not again
            log.warning("idle compact of chat %s failed: %s", sid, exc)
            return {"session": sid, "error": str(exc)[:200]}
        if got.get("compacted"):
            after = chat_transcript.stamp(sid)
            if after is not None:
                _TRIED[str(sid)] = after
            log.info("idle compact: chat %s %s → %s chars", sid,
                     got.get("before"), got.get("after"))
        return {"session": sid, **got}
    return {}


def register() -> None:
    """Every five minutes. ``AIFORGE_IDLE_COMPACT=0`` turns it off."""
    if os.environ.get("AIFORGE_IDLE_COMPACT", "1").strip().lower() in (
            "0", "false", "no", "off"):
        return
    from aiforge_core.runtime import periodic
    periodic.register("idle-compact", sweep, every_s=_EVERY_S)


def _reset_for_tests() -> None:
    _TRIED.clear()


__all__ = ["idle_minutes", "register", "sweep"]
