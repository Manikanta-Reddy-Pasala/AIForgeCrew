"""Rule capture off the turn's critical path, for a message that carries a task.

The capture classify used to run before routing on every message with a cue
word ("I want", "do not", "rule", …), bounded at 6s. A long request is not a
pure capture — it carries its own task — so nothing the turn does waits on the
answer: the only reason to classify first is the pure-capture short circuit
("always use tabs" → ack, no agent), and that needs a short message.

So a long or multi-line message classifies AFTER the agent answered: it
starts when the agent's ``done`` passes (beside the end-of-turn suggestion),
and the ``captured`` pill follows if it lands within a short tail wait
(see below); otherwise it is stored in the background. Not earlier, even with free slots: measured on a 4-slot local
server, a capture running beside the agent's first call slowed that call ~2x —
a slot is a queue position, not spare compute. On a one-slot server it never
queues ahead of the turn. Stored only then, too: a project rule written into
the repo mid-run would show up in this turn's Changes diff.

A short message keeps the inline pass (it may be a pure capture).

The classify is a side call (llm/model_wait ``side_call``): on a model
outage it fails at once instead of waiting and firing late on recovery. The
end of the turn does not wait for it (``AIFORGE_CAPTURE_TAIL_WAIT_S``,
default 0): the pill is published to the run whenever the rule is stored
while the stream is still open. A turn that was Stopped stores nothing. A
classify that lands while the NEXT turn of the chat runs is stored when that
turn ends — a rule file written mid-run would show in its Changes diff.
"""
from __future__ import annotations

import concurrent.futures as _cf
import contextvars
import os
import threading
import time

from ._core import _af_log


def _inline_max_chars() -> int:
    try:
        return max(0, int(os.environ.get("AIFORGE_CAPTURE_INLINE_MAX_CHARS", "240")))
    except (TypeError, ValueError):
        return 240


def inline_needed(prompt: str) -> bool:
    """True when the message could be a pure capture (short, one or two lines)
    — only then does the turn wait on the classify before routing."""
    p = (prompt or "").strip()
    return len(p) <= _inline_max_chars() and p.count("\n") < 3


# ── turns of a chat: a late store waits for the running one to end ─────────

_TURNS: dict[str, set] = {}          # session → tokens of its running turns
_DEFERRED: dict[str, list] = {}      # session → stores waiting for idle
_TLOCK = threading.Lock()


def turn_started(session_id) -> object:
    """A turn of ``session_id`` began; its token for :func:`turn_ended`."""
    tok = object()
    with _TLOCK:
        _TURNS.setdefault(str(session_id), set()).add(tok)
    return tok


def turn_ended(session_id, tok) -> None:
    """That turn ended: stores deferred while it ran happen now."""
    key = str(session_id)
    with _TLOCK:
        live = _TURNS.get(key, set())
        live.discard(tok)
        todo = []
        if not live:
            _TURNS.pop(key, None)
            todo = _DEFERRED.pop(key, [])
    for fn in todo:
        _safe(fn)


def _when_idle(session_id, own_tok, fn) -> None:
    """Run ``fn`` now, unless ANOTHER turn of the chat is running: then when
    the last one ends."""
    key = str(session_id)
    with _TLOCK:
        others = _TURNS.get(key, set()) - {own_tok}
        if others:
            _DEFERRED.setdefault(key, []).append(fn)
            return
    _safe(fn)


def _safe(fn) -> None:
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 — capture never breaks a turn
        _af_log.debug("background capture failed: %s", exc)


def _stopped(session_id) -> bool:
    try:
        from aiforge_core.runtime import chat_cancel
        return session_id is not None and chat_cancel.is_cancelled(session_id)
    except Exception:  # noqa: BLE001
        return False


class BgCapture:
    """One background capture: its classify future (None until started)."""

    def __init__(self, prompt, cwd, session_id, repo, rc, run=None, tok=None):
        self.prompt, self.cwd, self.session_id = prompt, cwd, session_id
        self.repo, self.rc = repo, rc
        self.run, self.tok = run, tok
        self.future: "_cf.Future | None" = None
        self.finished = False
        self.dropped = False
        self.started_at = 0.0

    def start(self) -> None:
        if self.future is not None:
            return
        self.started_at = time.monotonic()
        fut: _cf.Future = _cf.Future()

        def _run():
            try:
                from aiforge_core.llm import model_wait
                classify = model_wait.side_call(self.rc.classify)
                fut.set_result(classify(self.prompt, repo=self.repo,
                                        session_id=self.session_id))
            except BaseException as exc:  # noqa: BLE001 — read by finish()
                fut.set_exception(exc)

        threading.Thread(target=contextvars.copy_context().run, args=(_run,),
                         name="aiforge-capture-bg", daemon=True).start()
        self.future = fut

    def _store(self, cls) -> "dict | None":
        if not cls or cls.get("category") == "none":
            return None
        stored = self.rc.store(cls, repo=self.repo, session_id=self.session_id,
                               repo_root=self.cwd)
        ev = {"type": "captured", "id": stored.get("id"),
              "category": cls["category"], "scope": cls["scope"],
              "text": cls.get("canonical", ""), "repo": self.repo}
        intent = self.rc.recognize_gate_intent(cls)
        if intent:
            ev["gate_intent"] = intent
        return ev

    def finish_now(self, wait_s: float = 0.0) -> "dict | None":
        """Store the classification if it lands within ``wait_s`` of its
        start; the ``captured`` event or None. A classify still running is left
        to :meth:`finish_later`."""
        if self.finished or self.future is None:
            return None
        if _stopped(self.session_id):
            self.finished = self.dropped = True    # Stop: the rule is dropped
            return None
        left = wait_s - (time.monotonic() - self.started_at)
        if left > 0:
            _cf.wait([self.future], timeout=left)
        if not self.future.done():
            return None
        self.finished = True
        try:
            return self._store(self.future.result())
        except Exception as exc:  # noqa: BLE001 — capture never breaks a turn
            _af_log.debug("background capture failed: %s", exc)
            return None

    def finish_later(self) -> None:
        """Start (if deferred) and store whenever the classify lands, off the
        turn — dropped when the turn was Stopped; held while a later turn of
        the chat runs. The pill goes to the run if its stream is still open."""
        if self.finished:
            return
        self.finished = True
        if _stopped(self.session_id):
            self.dropped = True
            return
        self.start()

        def _store_now(cls):
            ev = self._store(cls)
            if ev and self.run is not None:
                self.run.publish(ev)       # a no-op once the run is done

        def _done(fut):
            try:
                cls = fut.result()
            except Exception as exc:  # noqa: BLE001
                _af_log.debug("background capture failed: %s", exc)
                return
            _when_idle(self.session_id, self.tok, lambda: _store_now(cls))
        self.future.add_done_callback(_done)


def _tail_wait_s() -> float:
    try:
        return max(0.0, float(os.environ.get("AIFORGE_CAPTURE_TAIL_WAIT_S", "0")))
    except (TypeError, ValueError):
        return 0.0


def begin(pc) -> None:
    """Set ``pc._bg_capture`` for a message worth classifying. Nothing is sent
    yet: see :func:`kick`."""
    pc._bg_capture = None
    try:
        from aiforge_core.runtime import rule_capture as _rc
        if not _rc.should_classify(pc.prompt):
            return
        pc._bg_capture = BgCapture(pc.prompt, pc.cwd, pc.session_id,
                                   _rc.repo_key(pc.cwd) or "repo", _rc,
                                   run=getattr(pc, "run", None),
                                   tok=getattr(pc, "_capture_turn", None))
    except Exception as exc:  # noqa: BLE001
        _af_log.debug("background capture not set up: %s", exc)


def kick(pc) -> None:
    """The agent answered: start the classify now."""
    bg = getattr(pc, "_bg_capture", None)
    if bg is not None:
        bg.start()


def events(pc):
    """After the run: the ``captured`` pill when the classify lands in time."""
    bg = getattr(pc, "_bg_capture", None)
    if bg is None:
        return
    bg.start()
    ev = bg.finish_now(_tail_wait_s())
    if ev:
        yield ev


def start_turn(pc) -> None:
    """A turn of the chat began (every turn: see :func:`turn_started`)."""
    pc._capture_turn = turn_started(pc.session_id)


def flush(pc) -> None:
    """Turn over: whatever is still pending finishes in the background, and
    captures earlier turns deferred while this one ran are stored now."""
    bg = getattr(pc, "_bg_capture", None)
    if bg is not None:
        bg.finish_later()
    tok = getattr(pc, "_capture_turn", None)
    if tok is not None:
        pc._capture_turn = None
        turn_ended(pc.session_id, tok)


__all__ = ["begin", "events", "flush", "inline_needed", "kick", "start_turn",
           "turn_ended", "turn_started"]
