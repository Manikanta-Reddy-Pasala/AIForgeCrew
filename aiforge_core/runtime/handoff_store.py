"""The handoff survives the process: saved with the chat, read by the next turn.

``handoff.py`` writes down what a run knows (goal, what is verified done, what
failed, the last error, the next step) without a model call. On its own that
record lives only inside the run that built it, so a crash, a restart, a Stop
or a new message started again from raw history. This module keeps it:

* **Save**: a :class:`Recorder` rides the chat loop and refreshes the record at
  natural points (a board item closes, a full test run goes green, every N tool
  steps, a stuck restart) and when the turn ends, however it ends. Deterministic,
  bounded (:data:`MAX_BYTES`), and written with one SQL ``UPDATE`` on the chat
  row, so a reader sees the old record or the new one, never half of either.
* **Resume**: the next turn of the same chat reads it. A turn that ended
  unfinished (Stop, crash, give-up summary, open board items) is seeded with
  the handoff instead of a replay of its raw history; the raw text is saved
  under an offload id (``memory_lookup {"id": ...}``). Whether the next
  message continues that work is the MODEL's call: the record is always handed
  over, saying "if the new message continues this work carry on from NEXT, if
  it is about something else ignore this block". Under a bare go-ahead
  ("continue") it is the turn's starting point; under a message that names a
  task of its own it is marked as reference only (see
  ``handoff.REFERENCE_HEAD``). Only the user's explicit clean rerun and the
  TTL drop it unread. A turn that then changes
  nothing (a question, an unrelated remark) leaves the record for the next
  message; it is offered :data:`_MAX_OFFERS` times at most.
* **Pipeline**: ``failed_approaches`` and ``doer_handoff`` of a ticket or team
  run are kept in the ticket metadata (or the chat's record) so a resumed run
  and a retry after a node failure start from them.

``AIFORGE_CHAT_HANDOFF_PERSIST=0`` turns all of it off (the old behaviour).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time

from aiforge_core.runtime import handoff

log = logging.getLogger("aiforge.handoff_store")

#: Hard bound on one stored record (JSON bytes).
MAX_BYTES = 12_000
_VERSION = 1

#: Status of a record whose turn did not finish.
UNFINISHED = frozenset({"running", "stopped", "gave_up", "interrupted",
                        "restarted", "open"})


def enabled() -> bool:
    return os.environ.get("AIFORGE_CHAT_HANDOFF_PERSIST", "1").strip().lower() \
        not in ("0", "false", "no", "off")


def every() -> int:
    """Refresh the record after this many tool steps (0 = only at the other
    points)."""
    try:
        return max(0, int(os.environ.get("AIFORGE_CHAT_HANDOFF_EVERY", "15")))
    except ValueError:
        return 15


def _ttl_s() -> float:
    try:
        return max(0.0, float(os.environ.get("AIFORGE_CHAT_HANDOFF_TTL_H", "168"))) * 3600
    except ValueError:
        return 168 * 3600


# ── the record ──────────────────────────────────────────────────────────────

def _clip(v, n: int) -> str:
    return " ".join(str(v or "").split())[:n]


def _strs(items, cap: int, n: int) -> list:
    out = []
    for it in list(items or [])[-cap:]:
        t = _clip(it, n)
        if t and t not in out:
            out.append(t)
    return out


def bound(h: dict) -> dict:
    """``h`` cut to a fixed shape and to at most :data:`MAX_BYTES` of JSON."""
    out = {
        "v": _VERSION,
        "goal": str(h.get("goal") or "").strip()[:1200],
        "turn_prompt": str(h.get("turn_prompt") or "").strip()[:400],
        "done": _strs(h.get("done"), 20, 200),
        "open": _strs(h.get("open"), 12, 200),
        "files": _strs(h.get("files"), 20, 200),
        "failed": _strs(h.get("failed"), handoff.MAX_FAILED, 240),
        "steers": _strs(h.get("steers"), 3, 300),
        "error": _clip(h.get("error"), 300),
        "green": bool(h.get("green")),
        "status": str(h.get("status") or "running")[:20],
        "reason": _clip(h.get("reason"), 60),
        "steps": int(h.get("steps") or 0),
        "offload": str(h.get("offload") or "")[:40] or None,
        # Times it was handed to a next turn, and its status before that.
        "offers": int(h.get("offers") or 0),
        "was": str(h.get("was") or "")[:20] or None,
        "updated_at": float(h.get("updated_at") or time.time()),
    }
    if isinstance(h.get("pipeline"), dict):
        out["pipeline"] = _pipeline_part(h["pipeline"])
    for key, cap in (("done", 4), ("files", 4), ("failed", 3), ("open", 4)):
        while len(json.dumps(out, default=str)) > MAX_BYTES and len(out[key]) > cap:
            out[key] = out[key][1:] if key != "open" else out[key][:-1]
    while len(json.dumps(out, default=str)) > MAX_BYTES and out["goal"]:
        out["goal"] = out["goal"][:max(0, len(out["goal"]) // 2)]
    return out


def _pipeline_part(p: dict) -> dict:
    part = {"failed_approaches": _strs(p.get("failed_approaches"), handoff.MAX_FAILED, 240)}
    dh = p.get("doer_handoff")
    if isinstance(dh, dict):
        part["doer_handoff"] = {k: (_strs(v, 12, 200) if isinstance(v, list)
                                    else _clip(v, 400) if not isinstance(v, (bool, int, float))
                                    else v)
                                for k, v in list(dh.items())[:12]}
    return part


def save(session_id, h: dict) -> bool:
    """Write the chat's record. Never raises; False when it was not written."""
    if session_id is None or not enabled():
        return False
    try:
        from aiforge_core.runtime import chat_store
        rec = bound({**h, "updated_at": time.time()})
        return bool(chat_store.set_session_handoff(
            int(session_id), json.dumps(rec, ensure_ascii=False, default=str)))
    except Exception as exc:  # noqa: BLE001 — a checkpoint never breaks a turn
        log.debug("handoff save failed (session=%s): %s", session_id, exc)
        return False


def load(session_id) -> "dict | None":
    """The chat's record, or None (none saved, disabled, or unreadable)."""
    if session_id is None or not enabled():
        return None
    try:
        from aiforge_core.runtime import chat_store
        raw = chat_store.get_session_handoff(int(session_id))
        if not raw:
            return None
        h = json.loads(raw)
        return h if isinstance(h, dict) and h.get("v") == _VERSION else None
    except Exception:  # noqa: BLE001 — a damaged record reads as none
        return None


def clear(session_id) -> None:
    if session_id is None:
        return
    try:
        from aiforge_core.runtime import chat_store
        chat_store.set_session_handoff(int(session_id), None)
    except Exception:  # noqa: BLE001
        pass


def is_unfinished(h: "dict | None") -> bool:
    return bool(h) and h.get("status") in UNFINISHED


def view(session_id) -> dict:
    """What the API and the status answer show: the live record of a running
    chat (built now), else the saved one."""
    with _LIVE_LOCK:
        rec = _LIVE.get(session_id)
    h = None
    if rec is not None:
        try:
            h = bound({**rec.build(), "status": "running", "reason": "live"})
        except Exception:  # noqa: BLE001
            h = None
    h = h or load(session_id)
    return {"enabled": enabled(), "handoff": h,
            "unfinished": is_unfinished(h),
            "text": handoff.render(h, resumed=True) if h else ""}


def on_stop(session_id) -> None:
    """The user pressed Stop: the record says so now, without waiting for the
    run to reach its next step."""
    if not enabled() or session_id is None:
        return
    with _LIVE_LOCK:
        rec = _LIVE.get(session_id)
    if rec is not None:
        rec.outcome = rec.outcome or "stopped"
        rec.save("stop", status="stopped")
        return
    h = load(session_id)
    if is_unfinished(h):
        save(session_id, {**h, "status": "stopped", "reason": "stop"})


# ── the next message of a chat with unfinished work ─────────────────────────

#: Times the record is offered to a turn that then changes nothing, before it
#: is dropped (a chat that moved on is not followed by its old handoff forever).
_MAX_OFFERS = 2


def _norm(text: str) -> str:
    return " ".join((text or "").lower().split())


def decide(h: "dict | None", prompt: str, *, forced: "bool | None" = None) -> str:
    """``"resume"`` (hand the record to the next turn), ``"keep"`` or
    ``"clear"`` for the next message of a chat.

    Whether the message CONTINUES the unfinished work is not decided here: the
    record is handed over as reference and the model, which reads the message
    with the conversation, decides (see ``handoff.RESUME_HEAD``). Only what
    needs no reading of the message is decided: nothing unfinished, a record
    past its TTL, and the user's explicit clean rerun."""
    if not is_unfinished(h):
        return "clear" if h else "keep"
    if _ttl_s() and time.time() - float(h.get("updated_at") or 0) > _ttl_s():
        return "clear"
    if forced is False:
        return "clear"                      # the user asked for a clean rerun
    if forced is True:
        return "resume"
    return "resume" if (prompt or "").strip() else "keep"


def _names_no_task(prompt: str, h: dict) -> bool:
    """The message itself says nothing about the task ("continue", "yes do
    it", or the same words again): the run then keeps the saved goal as its
    request. Only this bookkeeping uses a word rule; whether the work is
    continued is the model's call."""
    if _norm(prompt) in (_norm(h.get("turn_prompt")), _norm(h.get("goal"))):
        return False                        # the same words ARE the task
    from aiforge_core.runtime.chat_agent._turn._goahead import is_go_ahead
    from aiforge_core.runtime.chat_resume import _CONTINUE_RE
    from aiforge_core.runtime.chat_router import wants_changes
    text = (prompt or "").strip()
    return bool(_CONTINUE_RE.match(text)
                or (is_go_ahead(text) and not wants_changes(text)))


# ── seeding the next turn ───────────────────────────────────────────────────

def _unfinished_tail(rows: list) -> int:
    """Index of the first row of the unfinished turns at the end of ``rows``
    (the new user message is the last row): everything after the last turn that
    ended with an answer."""
    from aiforge_core.runtime.chat_resume import _is_stopped
    j = len(rows) - 1
    k = j - 1
    while k >= 0:
        r = rows[k]
        if not isinstance(r, dict):
            k -= 1
            continue
        if r.get("role") == "assistant":
            if not _is_stopped(r):
                break
        elif r.get("role") == "user":
            j = k
        k -= 1
    return j


def seed_next_turn(session_id, rows: list, prompt: str, history: list,
                   *, forced: "bool | None" = None) -> str:
    """When the chat has unfinished work this message continues, rewrite
    ``history`` in place so the turn starts from the handoff, and return the
    handoff text ("" when nothing was seeded). A new task clears the record."""
    if not enabled() or session_id is None:
        return ""
    h = load(session_id)
    if h is None:
        return ""
    verdict = decide(h, prompt, forced=forced)
    if verdict == "clear":
        clear(session_id)
        return ""
    if verdict != "resume" or not history:
        return ""
    try:
        from aiforge_core.api.routes._chat._history import _chat_history_for_agent
        from aiforge_core.runtime import context_offload
        j = _unfinished_tail(rows)
        dropped = _chat_history_for_agent(rows[j:-1]) if j < len(rows) - 1 else []
        prefix = _chat_history_for_agent(rows[:j])
        oid = context_offload.save(context_offload.render(dropped)) if dropped else None
        if oid:
            h["offload"] = oid
        # How the record is PRESENTED depends on whether the message names a
        # task of its own; whether the work is continued stays the model's call.
        same = _norm(prompt) in (_norm(h.get("turn_prompt")), _norm(h.get("goal")))
        bare = forced is True or _names_no_task(prompt, h)
        text = handoff.render(h, resumed=True, reference=not (same or bare))
        if h.get("goal") and bare and not same:
            # The new message says nothing about the task ("continue"):
            # quote the goal so the run, and every condense, still knows it.
            from aiforge_core.runtime.chat_resume import REQUEST_CLOSE, REQUEST_OPEN
            text = f"{text}\n{REQUEST_OPEN}\n{h['goal'][:4000]}\n{REQUEST_CLOSE}"
        last = rows[-1] if rows and rows[-1].get("role") == "user" else None
        body = str((last or {}).get("content") or prompt)
        new = {"role": "user", "content": f"{body}\n\n---\n{text}"}
        if prefix and prefix[-1]["role"] == "user":
            prefix[-1] = {"role": "user", "content": prefix[-1]["content"] + "\n\n" + new["content"]}
        else:
            prefix.append(new)
        history[:] = prefix
        save(session_id, {**h, "status": "running", "reason": "resumed",
                          "was": h.get("status") if h.get("status") != "running"
                          else h.get("was") or "interrupted",
                          "offers": int(h.get("offers") or 0) + 1})
        return text
    except Exception as exc:  # noqa: BLE001 — never break a turn over this
        log.debug("handoff seed skipped: %s", exc)
        return ""


# ── the recorder: refresh the record while a chat turn runs ─────────────────

_LIVE: "dict[int, Recorder]" = {}
_LIVE_LOCK = threading.Lock()


class Recorder:
    """Rides one chat turn. ``observe`` sees every event the loop yields,
    ``finish`` runs when the turn ends. Everything is best-effort."""

    def __init__(self, st):
        self.st = st
        self.sid = getattr(st, "session_id", None)
        self.active = enabled() and self.sid is not None
        self.steps = 0
        self.outcome = None
        self.offload = None
        self.prior: dict = {}
        self.prior_status = "interrupted"
        self.last_error = ""
        self._closed = self._closed_count() if self.active else -1
        self._green = None
        if not self.active:
            return
        st.handoff_rec = self
        try:
            self._adopt_prior()
        except Exception:  # noqa: BLE001
            pass
        with _LIVE_LOCK:
            _LIVE[self.sid] = self

    def _adopt_prior(self) -> None:
        """A turn seeded from a handoff carries it on: what failed stays in
        ``failed_approaches`` and what was done stays done."""
        seeded = any(m.get("role") == "user" and handoff.MARK in str(m.get("content") or "")
                     for m in self.st.convo[1:])
        if not seeded:
            return
        prior = load(self.sid) or {}
        self.prior = prior
        # seed_next_turn marked it running; this is what it was before.
        self.prior_status = prior.get("was") or "interrupted"
        self.offload = prior.get("offload")
        self.last_error = prior.get("error") or ""
        for t in prior.get("failed") or []:
            handoff.record_failed(self.st, t)

    # -- building --

    def build(self) -> dict:
        st = self.st
        h = handoff.build_chat(st)
        prior = self.prior
        done = _strs([*(prior.get("done") or []), *h["done"]], 20, 200)
        open_ = [t for t in (h["open"] or [t for t in (prior.get("open") or [])]) if t not in done]
        steers = _strs([*(prior.get("steers") or []),
                        *(getattr(st, "steers", None) or [])], 3, 300)
        if h["error"]:
            self.last_error = h["error"]
        h["error"] = h["error"] or self.last_error
        return {**h, "done": done, "open": open_, "steers": steers,
                "goal": h["goal"] or prior.get("goal") or "",
                "turn_prompt": prior.get("turn_prompt") or h["goal"],
                "green": bool(getattr(st, "last_green_fp", None)),
                "steps": int(prior.get("steps") or 0) + self.steps,
                "offload": self.offload}

    def save(self, reason: str, status: str = "running", **extra) -> bool:
        if not self.active:
            return False
        try:
            return save(self.sid, {**self.build(), "status": status,
                                   "reason": reason, **extra})
        except Exception:  # noqa: BLE001
            return False

    # -- the loop's events --

    def _left_untouched(self) -> bool:
        """This turn was seeded with unfinished work and landed no edit."""
        return bool(self.prior and is_unfinished(self.prior)
                    and int(self.prior.get("offers") or 0) < _MAX_OFFERS
                    and not getattr(self.st, "edits_made", 0))

    def _closed_count(self) -> int:
        board = getattr(self.st, "board", None) or {}
        return sum(1 for it in board.values()
                   if it.get("status") in ("done", "skipped"))

    def observe(self, ev) -> None:
        if not self.active or not isinstance(ev, dict):
            return
        try:
            self._observe(ev)
        except Exception:  # noqa: BLE001 — recording never breaks the turn
            pass

    def _observe(self, ev: dict) -> None:
        kind = ev.get("type")
        why = None
        if kind == "tool":
            self.steps += 1
            n = every()
            if n and self.steps % n == 0:
                why = "steps"
        elif kind == "error" and str(ev.get("text") or "").lower().startswith("stopped by user"):
            self.outcome = "stopped"
        elif kind == "stopped":
            # llm_unavailable / llm_request_fails / pipeline_error: the turn did
            # not finish, and "send it again to continue" must find the handoff.
            self.outcome = "interrupted"
        elif kind == "message" and not ev.get("supplementary"):
            text = str(ev.get("text") or "")
            if ev.get("awaiting_input"):
                self.outcome = "awaiting"
            elif text.lstrip().startswith("(stopped"):
                self.outcome = "gave_up"
            elif ev.get("role") != "system" and self.outcome != "interrupted":
                self.outcome = "final"
        closed = self._closed_count()
        if closed > self._closed >= 0:
            why = "item_done"
        self._closed = closed
        green = getattr(self.st, "last_green_fp", None)
        if green and green != self._green:
            why = why or "green_tests"
        self._green = green
        if why:
            self.save(why)

    def note_restart(self, h: dict, oid: "str | None") -> None:
        """A stuck restart replaced the context: save what it was replaced by."""
        if not self.active:
            return
        if oid:
            self.offload = oid
        if h.get("error"):
            self.last_error = h["error"]
        # The restart wiped the transcript the error was read from: keep the
        # one the handoff was built with.
        extra = {"error": h["error"]} if h.get("error") else {}
        self.save("restart", status="restarted", **extra)

    def finish(self) -> None:
        if not self.active:
            return
        with _LIVE_LOCK:
            if _LIVE.get(self.sid) is self:
                del _LIVE[self.sid]
        try:
            self._finish()
        except Exception:  # noqa: BLE001
            pass

    def _finish(self) -> None:
        from aiforge_core.runtime.chat_agent._turn._tasks import open_planned, open_items
        board = getattr(self.st, "board", None) or {}
        outcome = self.outcome
        if outcome in ("final", "awaiting"):
            left = open_planned(board)
            if left:
                self.save("turn_end", status="open")
            elif outcome == "final" and self._left_untouched():
                # The turn was handed unfinished work and changed nothing (it
                # answered a question, or was about something else): the work
                # is still unfinished, and the next message is offered it.
                save(self.sid, {**self.prior, "status": self.prior_status})
            elif outcome == "final":
                clear(self.sid)             # finished: nothing to resume
            elif open_items(board):
                self.save("awaiting_input", status="open")
            else:
                clear(self.sid)
            return
        status = {"stopped": "stopped", "gave_up": "gave_up"}.get(outcome, "interrupted")
        self.save("turn_end" if outcome else "turn_closed", status=status)


# ── pipeline runs (tickets and team runs) ───────────────────────────────────

def pipeline_handoff(state) -> dict:
    """The Doer handoff of a pipeline run, from its state (no model call)."""
    get = state.get
    fail = get("_iter_fail")
    err = ""
    if isinstance(fail, (list, tuple)) and len(fail) == 3:
        err = str(fail[2] or fail[0] or "")
    return {
        "goal": str(get("raw_ask") or get("ticket_title") or "")[:1200],
        "failed": list(get("failed_approaches") or [])[-handoff.MAX_FAILED:],
        "error": err,
        "iterations": int(get("doer_iters", 0) or 0),
        "replans": int(get("replan_count", 0) or 0),
        "tests_ok": get("tests_ok"),
        "verdict": str(get("feedback_verdict") or "")[:300],
        "next": str(get("replan_note") or "")[:300],
    }


def seed_pipeline_state(state: dict, metadata: "dict | None") -> dict:
    """Start a ticket run from what an earlier run of it learned."""
    if not enabled() or not isinstance(metadata, dict):
        return state
    items = [str(t) for t in (metadata.get("failed_approaches") or []) if t]
    if items:
        kept: list = []
        for t in items:
            handoff.note_failed(kept, t)
        state["failed_approaches"] = kept
        state["failed_approaches_md"] = handoff.render_failed(kept)
    dh = metadata.get("doer_handoff")
    if isinstance(dh, dict) and dh:
        state["doer_handoff"] = dh
        if dh.get("next") and not state.get("replan_note"):
            state["replan_note"] = str(dh["next"])[:300]
    return state


def ticket_patch(state) -> dict:
    """The ticket metadata that carries a run's learning to its next run."""
    if not enabled() or not state:
        return {}
    failed = list(state.get("failed_approaches") or [])
    if not failed and not state.get("doer_handoff"):
        return {}
    return {"failed_approaches": failed[-handoff.MAX_FAILED:],
            "doer_handoff": pipeline_handoff(state)}


def persist_ticket(state) -> None:
    """Write the run's failed approaches to its ticket now, so a retry after a
    node failure (or a crash) keeps them. Best-effort."""
    try:
        tid = state.get("_ticket_id")
        patch = ticket_patch(state)
        if tid is None or not patch:
            return
        from aiforge_core.tickets import store as tickets_mod
        tickets_mod.patch_fields(int(tid), metadata_patch=patch)
    except Exception as exc:  # noqa: BLE001
        log.debug("ticket handoff persist failed: %s", exc)


def note_team_state(session_id, state: dict) -> None:
    """A team run in a chat: keep its failed approaches with the chat's record
    so a resumed team run (or a follow-up) starts from them."""
    if not enabled() or session_id is None or not state:
        return
    patch = ticket_patch(state)
    if not patch:
        return
    h = load(session_id) or {}
    save(session_id, {**h, "status": h.get("status") or "interrupted",
                      "goal": h.get("goal") or patch["doer_handoff"].get("goal", ""),
                      "failed": _strs([*(h.get("failed") or []), *patch["failed_approaches"]],
                                      handoff.MAX_FAILED, 240),
                      "pipeline": {"failed_approaches": patch["failed_approaches"],
                                   "doer_handoff": patch["doer_handoff"]}})


def close_team_turn(session_id, goal: str, items, run_ok: bool,
                    cancelled: bool) -> None:
    """A team turn ended: finished work leaves no record; a stopped, failed or
    half-done one leaves the handoff the next turn resumes from."""
    if not enabled() or session_id is None:
        return
    prev = load(session_id) or {}
    rows = [i for i in (items or []) if isinstance(i, dict)]
    title = lambda i: str(i.get("goal") or i.get("title") or i.get("slug") or "")  # noqa: E731
    done = [title(i) for i in rows if str(i.get("status")).lower() in ("done", "skipped")]
    open_ = [title(i) for i in rows if str(i.get("status")).lower()
             not in ("done", "skipped", "failed")]
    if cancelled:
        status = "stopped"
    elif not run_ok:
        status = "gave_up"
    elif open_:
        status = "open"
    else:
        clear(session_id)
        return
    save(session_id, {**prev, "goal": prev.get("goal") or goal,
                      "turn_prompt": goal, "done": done, "open": open_,
                      "status": status, "reason": "team_turn_end"})


def team_seed(session_id) -> dict:
    """Initial team-run state from the chat's record (failed approaches and the
    Doer handoff), or {}."""
    h = load(session_id) if session_id is not None else None
    p = (h or {}).get("pipeline")
    if not isinstance(p, dict) or not is_unfinished(h):
        return {}
    return seed_pipeline_state({}, {"failed_approaches": p.get("failed_approaches"),
                                    "doer_handoff": p.get("doer_handoff")})


__all__ = ["close_team_turn", "on_stop", "enabled", "every", "bound", "save", "load", "clear", "view",
           "decide", "seed_next_turn", "Recorder", "pipeline_handoff",
           "seed_pipeline_state", "ticket_patch", "persist_ticket",
           "note_team_state", "team_seed", "MAX_BYTES", "UNFINISHED"]
