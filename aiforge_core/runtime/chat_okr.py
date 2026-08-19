"""Session-end OKR compaction: a chat session → scoped OKR briefs.

:mod:`chat_summary` keeps ONE browsable summary per session; this module does
the KNOWLEDGE fold. At session end (idle timeout or an explicit request) the
transcript is distilled by the learner LLM into ATOMIC durable items — decisions,
learnings, gotchas, config/stack facts, tickets, and MEANINGFUL user inputs
(preferences/corrections/instructions) — with chit-chat and transient status
dropped. Each item is routed to its scope (global / project / topic) via
:func:`md_store.classify_scope` and folded into the matching OKR brief through
:func:`md_store.capture` (which maintains ``compacted-<scope>.md``).

Trigger is config-driven (``AIFORGE_SESSION_COMPACT`` = ``idle`` | ``turns`` |
``explicit`` | ``off``); this module is the DOER — the daemon/endpoint decides
WHEN to call it. Soft-fail everywhere: background upkeep must never raise.

Env:
  AIFORGE_SESSION_COMPACT        idle (default) | turns | explicit | off
  AIFORGE_SESSION_COMPACT_CHARS  transcript chars per extraction window.
      Unset = sized from the ROLE'S context (see _window_chars), capped at
      _WINDOW_CEILING; a longer session is walked window by window.
  AIFORGE_SESSION_COMPACT_MAX_TOKENS  extraction reply cap (default 2000)
  AIFORGE_CONSOLIDATE_CTX_TOKENS  (shared with work_notes) overrides the role
      context the window is derived from.
"""
from __future__ import annotations

import logging
import os
import threading

from aiforge_core.config import _atomic

log = logging.getLogger("aiforge.chat_okr")

# PER-SESSION lock: serializes folds (+ the message snapshot) of the SAME
# session so a create-fold racing a delete-fold can't double-capture, WITHOUT a
# single global lock stalling an unrelated session's delete behind a slow fold's
# LLM calls. Named _FOLD_* (not _COMPACT_*) to avoid confusion with the distinct
# md_store._COMPACT_LOCK.
#
# REFCOUNTED so the map can't grow unbounded (one lock per session id ever seen):
# each entry is ``[Lock, refcount]``; a session's entry is removed the moment its
# last holder exits. While ANY holder references the key the SAME lock object is
# reused (correct mutual exclusion); when refcount hits 0 no one holds it, so
# deleting it is safe (a later fold just makes a fresh lock).
_FOLD_LOCKS: "dict[str, list]" = {}
_FOLD_LOCKS_GUARD = threading.Lock()
# Short-held lock JUST for the SHARED marker file's read-modify-write — different
# sessions' per-session locks don't serialize that shared file, so without this
# two sessions folding at once would clobber each other's offset (lost update).
_MARKER_LOCK = threading.Lock()
# Bumped by clear_all_markers (a cross-session reset that can't hold every
# session's fold lock). A fold that SNAPSHOTTED the epoch before a reset must not
# write its now-stale offset back afterwards (which would resurrect a cleared
# entry → a reused session id under-folds). Read/written under _MARKER_LOCK.
_RESET_EPOCH = 0


class _SessionFoldLock:
    """A refcounted per-session lock as a context manager (see _FOLD_LOCKS)."""

    def __init__(self, key: str):
        self._key = key
        self._lock: "threading.Lock | None" = None

    def __enter__(self):
        with _FOLD_LOCKS_GUARD:
            ent = _FOLD_LOCKS.get(self._key)
            if ent is None:
                ent = _FOLD_LOCKS[self._key] = [threading.Lock(), 0]
            ent[1] += 1
            self._lock = ent[0]
        try:
            self._lock.acquire()
        except BaseException:
            # __exit__ is NOT called when __enter__ raises (e.g. a signal /
            # KeyboardInterrupt during the blocking acquire), so balance the
            # refcount here or the entry leaks forever.
            self._release_refcount()
            self._lock = None
            raise
        return self

    def _release_refcount(self):
        with _FOLD_LOCKS_GUARD:
            ent = _FOLD_LOCKS.get(self._key)
            if ent is not None:
                ent[1] -= 1
                if ent[1] <= 0:
                    _FOLD_LOCKS.pop(self._key, None)

    def __exit__(self, *exc):
        if self._lock is not None:
            self._lock.release()
            self._release_refcount()
        return False


def _fold_lock(session_id) -> "_SessionFoldLock":
    return _SessionFoldLock(str(session_id))

_EXTRACT_SYS = (
    "You distil a chat session between an engineer and an AI assistant into "
    "ATOMIC durable knowledge items worth remembering across sessions. Keep "
    "ONLY meaningful content: decisions, conventions, learnings, gotchas, "
    "config/stack facts, tickets worked, and MEANINGFUL user inputs "
    "(preferences, corrections, instructions the user gave). DROP pleasantries, "
    "small talk, transient status, and anything trivial or already obvious. "
    "Each item is ONE concise sentence tagged with a kind:\n"
    "  learning          a general lesson (applies across projects)\n"
    "  project_learning  a lesson about ONE repository/service\n"
    "  topic_learning    a lesson about a cross-cutting theme/workflow\n"
    "  user_comment      a meaningful thing the USER said to keep (intent/preference)\n"
    "PRESERVE EXACT IDENTIFIERS verbatim — jira/issue keys (ONE-3), version "
    "numbers, file paths, commands, config values, ports, error codes. Never "
    "generalize away or reword an id.\n"
    "Return an items list; empty if nothing durable was said."
)


def _disabled() -> bool:
    return os.environ.get("AIFORGE_SESSION_COMPACT", "idle") in (
        "off", "0", "false", "no")


def _int_env(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except (TypeError, ValueError):
        return default


# Ceiling on ONE extraction window. Not a context limit — a quality/latency one:
# beyond ~30 items per call the model starts dropping items rather than listing
# them, and a failed window costs the whole slice.
_WINDOW_CEILING = 24000


def _window_chars(role: str) -> int:
    """Chars of transcript per extraction call.

    Sized from the ROLE'S OWN WINDOW rather than a flat 8000: the learner has a
    16k-token context, so an 8k-char window (~2k tokens) spent 5 calls where one
    would do. An explicit AIFORGE_SESSION_COMPACT_CHARS still wins.
    """
    raw = os.environ.get("AIFORGE_SESSION_COMPACT_CHARS")
    if raw:
        return max(500, _int_env("AIFORGE_SESSION_COMPACT_CHARS", 8000))
    try:
        from aiforge_core.runtime.work_notes._consolidate import input_char_budget
        out_tokens = _int_env("AIFORGE_SESSION_COMPACT_MAX_TOKENS", 2000)
        budget = input_char_budget(role, output_tokens=out_tokens)
        room = budget - len(_EXTRACT_SYS) - 512
        # CLAMP, don't floor: input_char_budget floors at 4000 chars, so a large
        # MAX_TOKENS against a small role window would hand back more room than
        # exists and every call would 400 — which, being deterministic, is the
        # one failure the window walk cannot retry its way out of.
        return max(500, min(_WINDOW_CEILING, room))
    except Exception:  # noqa: BLE001 — unknown window → the old flat default
        return 8000


def _transcript(turns: list[dict], limit: int,
                start_char: int = 0) -> "tuple[str, int, int]":
    """Compact ``ROLE: content`` transcript of the OLDEST turns that fit in
    ``limit`` chars → ``(text, turns_consumed, part_offset)``.

    Head-first, not tail-first. The tail carries the outcome, but the durable
    offset then jumps past every turn — so a cut head is folded by nobody, ever
    (worst on a long single-chat day: one 8k window over ~110k chars of
    transcript). Taking the oldest turns lets the caller fold the rest in the
    next window instead of losing it.

    A turn BIGGER than one window (a tool dump, a long answer) is walked in
    slices: ``start_char`` is where to resume inside it and the returned
    ``part_offset`` is where the next window resumes — 0 once the turn is fully
    consumed. Clipping it once and marking it folded would drop most of it.
    """
    lines: list[str] = []
    used = 0
    taken = 0
    part = 0
    for n, m in enumerate(turns):
        role = (m.get("role") or "user").strip().upper()
        content = (m.get("content") or "").strip()
        if n == 0 and start_char:
            content = content[start_char:]
        if not content:
            taken += 1                      # empty turn: consumed, nothing to say
            continue
        line = f"{role}: {content}"
        sep = 2 if lines else 0
        if lines and used + sep + len(line) > limit:
            break
        if not lines and len(line) > limit:
            # OVERSIZED TURN — emit a slice and remember where to resume; the
            # turn is not consumed until its last slice has been sent.
            head = f"{role}: "
            body_room = max(1, limit - len(head))
            line = head + content[:body_room]
            lines.append(line)
            part = (start_char or 0) + body_room
            return "\n\n".join(lines), 0, part
        lines.append(line)
        used += sep + len(line)
        taken += 1
    return "\n\n".join(lines), taken, 0


def _extract(transcript: str, role: str) -> "list | None":
    """LLM → list of items (each ``.text`` + ``.kind``); **None on failure**.

    The empty list means "nothing durable in these turns" (this is also the
    MEANINGFUL-input filter — the prompt drops chit-chat); None means the model
    never answered. The caller must not advance the durable offset on None, or
    one provider hiccup silently marks a whole window as folded with zero
    captures.
    """
    try:
        from pydantic import BaseModel

        from aiforge_core.llm.structured import structured_complete

        class SessionItem(BaseModel):
            text: str = ""
            kind: str = "learning"

        class SessionItems(BaseModel):
            items: list[SessionItem] = []

        res = structured_complete(
            role,
            [{"role": "system", "content": _EXTRACT_SYS},
             # NO extra truncation here: the caller sized the window and the
             # durable offset advances over exactly those turns, so a second,
             # smaller cap would mark turns folded that the model never saw.
             {"role": "user", "content": transcript}],
            SessionItems,
            max_tokens=_int_env("AIFORGE_SESSION_COMPACT_MAX_TOKENS", 2000),
            max_retries=1, temperature=0.0)
        return list(getattr(res, "items", None) or [])
    except Exception as exc:  # noqa: BLE001 — model down → retry next pass
        log.warning("chat_okr extract failed (offset not advanced): %s", exc)
        return None


def _marker_path():
    from aiforge_core.memory import md_store
    return md_store.memory_dir() / ".session_okr_marker.json"


def forget_session(session_id) -> None:
    """Drop a session's compaction-offset marker entry — called when the session
    is deleted so the marker file doesn't grow unbounded across many sessions.
    Takes the session's fold lock (so it can't race a concurrent fold of the
    same session) + the marker lock (shared file). Never raises."""
    try:
        with _fold_lock(session_id), _MARKER_LOCK:
            marker = _load_marker()
            if marker.pop(str(session_id), None) is not None:
                _save_marker(marker)
    except Exception as exc:  # noqa: BLE001
        log.debug("forget_session(%s) failed: %s", session_id, exc)


def clear_all_markers() -> None:
    """Wipe ALL compaction offsets — used when every session is reset/deleted in
    bulk. Session ids restart at 1 after a reset, so a leftover offset would make
    a NEW session id-1 read a stale high-water mark and skip folding (silent
    knowledge loss). Under the marker lock. Never raises."""
    global _RESET_EPOCH
    try:
        with _MARKER_LOCK:
            _RESET_EPOCH += 1
            _save_marker({})
    except Exception as exc:  # noqa: BLE001
        log.debug("clear_all_markers failed: %s", exc)


# A window that fails to extract this many times in a row is SKIPPED (logged),
# so a poison window — an output the model always truncates on, a content
# filter, a per-message size limit — cannot wedge a session forever.
_MAX_WINDOW_FAILS = 3
# …and two failures closer together than this count as ONE. The counter is
# durable and every caller bumps it (the daily pass, a chat switch, the explicit
# endpoint), so three clicks during a two-minute provider hiccup would otherwise
# discard a window of turns that nothing was ever wrong with.
_WINDOW_FAIL_SPACING_S = 3600


def _entry(marker: dict, session_id) -> dict:
    """One session's marker entry → ``{offset, part, fails}``.

    Back-compatible with the old bare int (the turn offset). ``part`` is the
    character offset INSIDE the next turn, used when a single turn is larger
    than one window."""
    raw = marker.get(str(session_id), 0)
    if isinstance(raw, int):
        raw = {"offset": raw}
    if not isinstance(raw, dict):
        raw = {}

    def _n(key):
        v = raw.get(key, 0)
        return v if isinstance(v, int) and v >= 0 else 0

    return {"offset": _n("offset"), "part": _n("part"), "fails": _n("fails"),
            "fail_at": _n("fail_at")}          # epoch seconds of the last failure


def _put_entry(marker: dict, session_id, entry: dict) -> None:
    entry.setdefault("fail_at", 0)
    if entry["part"] or entry["fails"]:
        marker[str(session_id)] = entry
    else:               # the common shape stays a bare int (older readers cope)
        marker[str(session_id)] = entry["offset"]


def _load_marker() -> dict:
    import json
    try:
        with open(_marker_path(), encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_marker(d: dict) -> None:
    import contextlib
    import json
    with contextlib.suppress(OSError):
        _atomic.write_text(_marker_path(), json.dumps(d))


def compact_session(session_id, *, repo: str | None = None,
                    role: str = "learner", min_turns: int = 2) -> dict:
    """Distil ONE session into scoped OKR briefs. Never raises.

    Only messages AFTER the last-compacted offset are re-extracted (a durable
    per-session marker), so a re-compaction on restart or when more messages
    arrive doesn't re-distil — and re-capture reworded duplicates of — the
    already-folded earlier turns.

    Returns ``{"ok": bool, "captured": int, ...}`` — ``skipped`` for disabled /
    too-short / no-new sessions."""
    if _disabled():
        return {"ok": False, "skipped": "disabled", "captured": 0}
    try:
        from aiforge_core.memory import md_store
        from aiforge_core.runtime import chat_store
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "captured": 0}

    # PER-SESSION lock spans the message SNAPSHOT → capture → offset write, so a
    # concurrent fold of the SAME session can't act on a stale pre-delete
    # snapshot (resurrection) or double-capture. Different sessions fold + delete
    # concurrently — no global stall.
    with _fold_lock(session_id):
        # Snapshot the reset epoch BEFORE reading messages, so ANY reset that
        # lands after this data snapshot advances the epoch past epoch0 and trips
        # the write-back skip (snapshotting it later left a window where a reset
        # between the message read and the epoch read went undetected).
        with _MARKER_LOCK:
            epoch0 = _RESET_EPOCH
        try:
            messages = chat_store.get_messages(session_id)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc), "captured": 0}
        turns = [m for m in (messages or [])
                 if isinstance(m, dict) and m.get("role") in ("user", "assistant")
                 and (m.get("content") or "").strip()]
        if len(turns) < max(1, int(min_turns)):
            return {"ok": True, "skipped": "too_short", "captured": 0}

        # Durable offset: distil only turns NEW since the last compaction. The
        # marker lock is short — a DIFFERENT session mustn't clobber the shared
        # file (lost update) — but is released before the slow LLM work.
        with _MARKER_LOCK:
            marker = _load_marker()
            entry = _entry(marker, session_id)
        last, part, fails = entry["offset"], entry["part"], entry["fails"]
        fail_at = entry["fail_at"]
        if len(turns) <= last:
            return {"ok": True, "skipped": "no_new", "captured": 0}
        new_turns = turns[last:]

        transcript, taken, next_part = _transcript(new_turns, _window_chars(role),
                                                   start_char=part)
        items = _extract(transcript, role)
        if items is None:
            # Model down — soft-fail (callers treat a RAISE as a real error) and
            # do NOT advance: these turns are folded on the next pass. But count
            # the failure: a window the model can never answer (truncated output,
            # a filter, a size limit) is deterministic, so without a bound the
            # same window is retried every pass, forever, capturing nothing.
            import time as _time
            now_s = int(_time.time())
            if now_s - fail_at >= _WINDOW_FAIL_SPACING_S:
                fails += 1                    # a burst of failures counts once
                fail_at = now_s
            give_up = fails >= _MAX_WINDOW_FAILS
            if give_up:
                log.warning("chat_okr: session=%s window at offset %d failed %d "
                            "times — skipping it to keep the session moving",
                            session_id, last, fails)
            with _MARKER_LOCK:
                if _RESET_EPOCH == epoch0:
                    marker = _load_marker()
                    _put_entry(marker, session_id,
                               {"offset": (last + max(1, taken)) if give_up else last,
                                "part": 0 if give_up else part,
                                "fails": 0 if give_up else fails,
                                "fail_at": 0 if give_up else fail_at})
                    _save_marker(marker)
            return {"ok": True, "skipped": "extract_failed", "captured": 0,
                    "remaining": len(new_turns)}
        captured = 0
        rows = [((getattr(it, "text", "") or "").strip(),
                 (getattr(it, "kind", "") or "learning").strip()) for it in items]
        rows = [r for r in rows if r[0]]
        # ONE scope call for the whole window, not one per item: per-item calls
        # re-sent the classifier's rule prompt for every fact and made scoping
        # ~90% of a fold's LLM traffic. Guarded because this function must never
        # raise, and a scope failure must not cost the whole window's items.
        try:
            scopes = md_store.classify_scopes([t for t, _ in rows], hint_repo=repo,
                                              role=role)
        except Exception as exc:  # noqa: BLE001
            log.warning("chat_okr scope classification failed: %s", exc)
            scopes = [{"scope": "project", "repo": repo, "topic": None}
                      for _ in rows]
        for (text, kind), sc in zip(rows, scopes):
            try:
                md_store.capture(kind, text, repo=sc["repo"], topic=sc["topic"],
                                 classify=False, source=f"chat-session:{session_id}")
                captured += 1
            except Exception as exc:  # noqa: BLE001 — one bad item never aborts the fold
                log.debug("chat_okr capture failed: %s", exc)
        # Advance the durable offset (re-read under the marker lock so a
        # concurrent different-session write isn't clobbered). But if a reset
        # (clear_all_markers) ran while we did the slow LLM work, our offset is
        # stale — writing it would resurrect a cleared entry and make a reused
        # session id under-fold; skip the write-back in that case.
        if rows and captured == 0:
            # The model answered but NOTHING was stored (md_store/sqlite down,
            # disk full). Advancing here would mark the window folded with zero
            # captures — the same loss the None-on-extract-failure guard exists
            # to prevent, from the write side instead of the read side.
            log.warning("chat_okr: session=%s captured 0 of %d items — offset held",
                        session_id, len(rows))
            return {"ok": True, "skipped": "capture_failed", "captured": 0,
                    "remaining": len(new_turns)}
        if captured < len(rows):
            log.warning("chat_okr: session=%s captured %d of %d items",
                        session_id, captured, len(rows))
        with _MARKER_LOCK:
            if _RESET_EPOCH != epoch0:
                return {"ok": True, "captured": captured, "skipped": "reset"}
            marker = _load_marker()
            if next_part:                    # mid-way through an oversized turn
                offset = last
            else:
                offset = min(last + max(1, taken), len(turns))
            _put_entry(marker, session_id, {"offset": offset, "part": next_part,
                                            "fails": 0, "fail_at": 0})
            _save_marker(marker)
    remaining = max(0, len(turns) - offset) if not next_part \
        else max(1, len(turns) - offset)
    log.info("chat_okr: session=%s repo=%s captured=%d (offset→%d part=%d, "
             "remaining=%d)", session_id, repo, captured, offset, next_part,
             remaining)
    return {"ok": True, "captured": captured, "remaining": remaining}


def previous_session_id(exclude_session_id):
    """The id of the MOST RECENT prior session (excluding the current), or None.
    Used so recall can exclude exactly what previous_session_brief already
    injects — without dropping OLDER sessions' recall. Never raises."""
    try:
        from aiforge_core.runtime import chat_store
        for s in (chat_store.list_sessions() or []):   # newest-first
            sid = (s or {}).get("id")
            if sid is not None and sid != exclude_session_id:
                return sid
    except Exception:  # noqa: BLE001
        pass
    return None


def previous_session_brief(exclude_session_id, *, max_turns: int = 6,
                           max_chars: int = 1200) -> str:
    """A short continuity block from the MOST RECENT prior session (excluding the
    current one) so the next chat carries the previous conversation forward. The
    block is explicitly framed as supersedable — if the user's new ask
    contradicts it, the new statement wins (and the OKR supersede policy applies
    at the next compaction). Deterministic (no LLM — the tail of the prior
    transcript). Empty when there is no prior session. Never raises."""
    try:
        from aiforge_core.runtime import chat_store
        sessions = chat_store.list_sessions() or []
    except Exception as exc:  # noqa: BLE001
        log.debug("previous_session_brief list_sessions failed: %s", exc)
        return ""
    prior_id = None
    for s in sessions:                       # list_sessions is newest-first
        sid = (s or {}).get("id")
        if sid is None or sid == exclude_session_id:
            continue
        prior_id = sid
        break
    if prior_id is None:
        return ""
    try:
        msgs = chat_store.get_messages(prior_id) or []
    except Exception as exc:  # noqa: BLE001
        log.debug("previous_session_brief get_messages failed: %s", exc)
        return ""
    turns = [m for m in msgs if isinstance(m, dict)
             and m.get("role") in ("user", "assistant")
             and (m.get("content") or "").strip()]
    if not turns:
        return ""
    lines = [f"PREVIOUS SESSION {prior_id} (continuity — if the user's new ask "
             "contradicts this, the new statement supersedes it):"]
    for m in turns[-max(1, max_turns):]:
        role = (m.get("role") or "user").strip().upper()
        content = " ".join((m.get("content") or "").split())
        lines.append(f"{role}: {content}")
    return "\n".join(lines)[:max_chars]


__all__ = ["compact_session", "previous_session_brief", "forget_session",
           "clear_all_markers"]
