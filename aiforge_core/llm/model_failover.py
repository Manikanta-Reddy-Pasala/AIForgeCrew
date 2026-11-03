"""Chat failover: when the chat's model stops answering, the same request goes
to the next model the owner listed (config/chat_failover), and the turn stays
there.

Without this a model that went silent held the turn for the whole stall
ladder: 180 s for the first token, the same request again with doubled bounds,
then a wait for the model to "come back" — forever by default. The registry
chain (client/_chain) only runs after ALL of that has failed, and the
provider fallback (router.fallback) needs a second provider. On a box with
several models configured the user sat watching "no output for 9 minutes".

One :class:`Guard` per logical POST (client/_http_retry), built only inside a
chat turn. It does three things, all predictable:

* **Sticky** — once a turn has failed over from X to Y, every later request of
  the turn that resolves to X is sent to Y (the record lives in the turn's
  shared request_health state, so every step thread sees it). No flapping back.
* **Stall** — while a next model exists the stream health bounds
  (client/_stream_health) are capped at ``chat_failover.stall_s()``: no first
  token, or no new token, for that long ends the send with the usual
  ``LLMStreamStalled`` and the guard re-sends to the next model. Tokens arriving
  restart the idle clock, so a slow but streaming answer never fails over. On
  the LAST model nothing is capped: the existing stall / outage handling
  applies unchanged.
* **Transport** — ``chat_failover.fails()`` failures in a row (refused, reset,
  read timeout, 500/502/504), or an endpoint the breaker has written off, fail
  over too. A 429 or a "loading" 503 never does: that is the server's state,
  and the rate limiter owns it.

The user changing the model mid-run (llm/model_switch) goes through the same
guard: the in-flight request is aborted and re-sent on the new pick, and the
turn's failover record is dropped (the user's choice wins).

Every re-send goes through the normal ``_post`` — rate limiter, preflight,
meter — so failover never sends faster than the ceiling allows.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import threading
from dataclasses import replace

from . import model_switch
from .model_switch import ModelSwitched

#: The guard of the POST running on this thread (stream bounds, abort check).
_CURRENT: contextvars.ContextVar = contextvars.ContextVar(
    "aiforge_failover_guard", default=None)

#: Guards the turn's shared failover record: the parallel step threads of one
#: turn (subtasks) read and extend it at the same time.
_FO_LOCK = threading.RLock()

#: Transport labels (client/_errors.is_transient_exc) that count as "the model
#: is not answering". Not rate_limited / http_503 / loading: busy, not dead.
_FAIL_LABELS = frozenset({"url_error", "os_error", "http_500",
                          "http_502", "http_504"})


def _key(model: str, base_url: str) -> str:
    u = str(base_url or "").strip().rstrip("/").lower()
    u = u[:-3] if u.endswith("/v1") else u
    return f"{str(model or '').strip().lower()}@{u}"


def _ep_key(ep) -> str:
    return _key(ep.model, ep.base_url)


def _retarget(payload: bytes, model: str) -> bytes:
    """The same request body for another model id (tools, messages and every
    other parameter unchanged — as the registry chain does it)."""
    try:
        body = json.loads(payload.decode())
    except Exception:  # noqa: BLE001 — not JSON: cannot be re-addressed
        return payload
    body["model"] = model
    return json.dumps(body).encode()


def _session_id():
    try:
        from aiforge_core.runtime import request_context
        return request_context.context_session_id()
    except Exception:  # noqa: BLE001
        return None


def _notify(text: str, state: str) -> None:
    """A line in the chat (the same sink the "sending again" lines use)."""
    try:
        from aiforge_core.llm import _wait_scope
        _wait_scope.notify({"type": "llm_wait", "state": state, "text": text})
    except Exception:  # noqa: BLE001 — a status line never breaks a call
        pass


def _phase(sid, model: str) -> None:
    """The run's "waiting on …" line names the model now asked, so a quiet
    spell after a switch does not read as the old model still hanging."""
    try:
        from aiforge_core.runtime import chat_runs
        key = int(sid) if str(sid).isdigit() else sid
        chat_runs.set_phase(key, f"waiting for {model} to answer")
    except Exception:  # noqa: BLE001
        pass


def _log(msg: str, *args) -> None:
    try:
        from aiforge_core.llm.client._helpers import _log as log
        log.warning(msg, *args)
    except Exception:  # noqa: BLE001
        pass


def _stall_of(exc: BaseException):
    """The stream stall behind ``exc`` (itself, or the one a spent stall budget
    wraps), or None. A stall because the machine slept is not the model's."""
    from aiforge_core.llm.client._stream_health import LLMStreamStalled
    cand = exc if isinstance(exc, LLMStreamStalled) else getattr(exc, "last", None)
    if isinstance(cand, LLMStreamStalled) and cand.phase != "after_sleep":
        return cand
    return None


def _fmt(s: float) -> str:
    return f"{s / 60:.0f} min" if s >= 90 else f"{s:.0f} s"


def _server_alive(ep, timeout_s: float = 5.0) -> bool:
    """:func:`_probe_models` on a side thread, given up the moment Stop is
    pressed (the chat's cancel token / a model-wait cancel)."""
    box: dict = {}
    t = threading.Thread(target=lambda: box.update(ok=_probe_models(ep, timeout_s)),
                         daemon=True)
    t.start()
    while t.is_alive():
        t.join(0.1)
        if _stopped():
            return False
    return bool(box.get("ok"))


def _stopped() -> bool:
    try:
        from aiforge_core.llm import _wait_scope
        return bool(_wait_scope.cancel_reason())
    except Exception:  # noqa: BLE001
        return False


def _probe_models(ep, timeout_s: float = 5.0) -> bool:
    """Does the model SERVER answer at all (``GET /models``, a few seconds)?
    Cheap and not queued behind the prompt it is prefilling, unlike a probe
    completion. Answering proves the box is up, not that the model will ever
    answer — hence only one extra send on it (Guard.take)."""
    import urllib.request
    try:
        from aiforge_core.llm.client._http import _post_ctx, _post_headers
        url = f"{str(ep.base_url).rstrip('/')}/models"
        req = urllib.request.Request(url, headers=_post_headers(ep), method="GET")
        with urllib.request.urlopen(req, timeout=timeout_s,
                                    context=_post_ctx(ep)) as resp:
            return 200 <= int(getattr(resp, "status", 200)) < 300
    except Exception:  # noqa: BLE001 — no answer is the answer
        return False


class Guard:
    """One logical POST's failover / switch state. ``ep`` and ``payload`` are
    what to send NOW (already moved to the turn's sticky model or the user's
    new pick)."""

    def __init__(self, ep, payload: bytes, role: str, source: str, sid,
                 turn: "dict | None") -> None:
        from aiforge_core.config import chat_failover
        self.ep, self.payload, self.role, self.sid = ep, payload, role, sid
        state = turn if turn is not None else {}
        self.fo = state.setdefault("failover", {"tried": [], "to": None,
                                                "switch_seq": 0})
        self.fails = 0
        self.alive_waits = 0       # first-token stalls let through on a live server
        self.seq0 = model_switch.seq_now(sid)
        # Whether this request follows the chat's pick is worked out only when
        # it matters (failover configured, or a switch to apply): with no
        # failover list a send does nothing here beyond the switch check. The
        # context is kept because the abort check runs on the watcher thread.
        self._ctx = contextvars.copy_context()
        self._follows_v = None
        self._flock = threading.Lock()
        # Only a request that runs on the chat's model fails over to the chat's
        # failover list: a dedicated coder / vision role keeps its own model.
        self.on = (chat_failover.enabled_by_env() and bool(chat_failover.model_ids())
                   and self._eligible(role, source) and self.follows)
        model_switch.enter(sid)
        self._apply_pending_switch()
        if self.on:
            self._apply_sticky()

    @property
    def follows(self) -> bool:
        """Does this request run on the chat's pick? Computed once, lazily."""
        with self._flock:
            if self._follows_v is None:
                try:
                    self._follows_v = bool(self._ctx.run(self._follows))
                except Exception:  # noqa: BLE001
                    self._follows_v = False
            return self._follows_v

    # — who this applies to ——————————————————————————————————————————
    def _eligible(self, role: str, source: str) -> bool:
        try:
            from aiforge_core.llm import _wait_scope
            if _wait_scope._OPTIONAL.get():
                return False        # a bounded side call fails fast instead
            from aiforge_core.llm._rate_settings import category
            if category(role) == "compaction":
                return False        # background memory work is not the chat
        except Exception:  # noqa: BLE001
            return False
        if str(source or "").startswith("model_chain"):
            return False            # the registry chain is one try per model
        # A vision request reached its model because that model can see.
        return b'"image_url"' not in (self.payload or b"")

    def _follows(self) -> bool:
        """Does this request run on the chat's pick (so a user switch applies)?"""
        if self.role in ("chat", "_default"):
            return True
        try:
            # A role with a model of its own keeps it whatever the chat picks
            # (config/chat_models.apply) — a user switch must not cut it.
            from aiforge_core.config.agent_config._resolve import _roles_with_own_model
            if self.role in _roles_with_own_model():
                return False
        except Exception:  # noqa: BLE001
            pass
        sw = model_switch.latest(self.sid) or {}
        if self.ep.model in (sw.get("from"), sw.get("to")):
            return True
        with _FO_LOCK:
            if _ep_key(self.ep) in self.fo.get("tried", []):
                return True
        try:
            from aiforge_core.llm.router import resolve
            return resolve("chat").model == self.ep.model
        except Exception:  # noqa: BLE001
            return False

    # — stickiness and the user's pick ————————————————————————————————
    def _row_endpoint(self, row: dict):
        from aiforge_core.llm.client._chain import _chain_endpoint
        return _chain_endpoint(row, self.ep)

    def _move_to(self, ep) -> None:
        self.ep, self.payload = ep, _retarget(self.payload, ep.model)
        self.fails = self.alive_waits = 0
        # A new model starts clean: the old one's stalls must not double the
        # new one's bounds or spend its share of the turn's stall budget.
        try:
            from aiforge_core.llm import request_health
            h = request_health.current()
            if h is not None:
                h.stalls = h.stalls_seen = h.stalls_counted = 0
                h.stall_s = 0.0
            request_health.note_turn_answer()
        except Exception:  # noqa: BLE001
            pass

    def _clear_failover(self, seq: int) -> None:
        """The user picked a model: the turn's failover record goes."""
        with _FO_LOCK:
            self.fo["tried"], self.fo["to"] = [], None
            self.fo["switch_seq"] = max(int(self.fo.get("switch_seq", 0)), int(seq))
        self.seq0 = max(self.seq0, int(seq))

    def _apply_sticky(self) -> None:
        with _FO_LOCK:
            to = self.fo.get("to")
            tried = list(self.fo.get("tried", []))
        if not to or _ep_key(self.ep) not in tried:
            return
        ep = self._row_endpoint(to)
        if ep is not None and _ep_key(ep) not in tried:
            self._move_to(ep)

    def _apply_pending_switch(self) -> None:
        """A switch this turn has not applied yet, and this request was built
        for the old model (resolved just before the pick changed): send it on
        the new one. Silent — the step has not been tried yet. Either way the
        turn's failover record goes: the user's pick wins over it."""
        sw = model_switch.latest(self.sid)
        if not sw or sw["seq"] <= self.fo.get("switch_seq", 0) or not self.follows:
            return
        if model_switch.same_endpoint(sw["from"], sw.get("from_url"),
                                      self.ep.model, self.ep.base_url) \
                and not self._is_pick(sw):
            self._user_switch(sw, say=False)
        else:
            self._clear_failover(sw["seq"])

    def _is_pick(self, sw: dict) -> bool:
        """Is this request already on the switch's new model (endpoint too)?"""
        return model_switch.same_endpoint(sw["to"], sw.get("to_url"),
                                          self.ep.model, self.ep.base_url)

    def _resolve_pick(self):
        from aiforge_core.llm.router import resolve
        new = resolve(self.role)
        if new.model == self.ep.model and new.base_url == self.ep.base_url:
            new = replace(resolve("chat"), role=self.ep.role)
        return new

    def _user_switch(self, sw: dict, say: bool = True) -> bool:
        self._clear_failover(sw["seq"])     # the user's choice wins
        try:
            new = self._resolve_pick()
        except Exception:  # noqa: BLE001 — cannot resolve: retry where we were
            return say
        if new.model == self.ep.model and new.base_url == self.ep.base_url:
            return say
        old = self.ep.model
        self._move_to(new)
        _log("llm.model_switch role=%s from=%s to=%s (picked mid-run)",
             self.role, old, new.model)
        if say:
            _notify(f"↻ switched to {new.model} — retrying the step", "switched")
            _phase(self.sid, new.model)
        return True

    # — the transport's questions ——————————————————————————————————————
    def abort(self) -> bool:
        """Must the in-flight send be cut NOW (the user picked another model
        after it started)? Polled by the cancellable POST's watcher thread."""
        sw = model_switch.latest(self.sid)
        if not sw or sw["seq"] <= self.seq0 or not self.follows:
            return False
        return not self._is_pick(sw)

    def candidates(self) -> list:
        """The listed models still to try, in list order starting after the
        current one; the current model and every model this turn already
        failed on are left out."""
        if not self.on:
            return []
        from aiforge_core.config import chat_failover
        rows = chat_failover.rows()
        cur = _ep_key(self.ep)
        eps = [(r, self._row_endpoint(r)) for r in rows]
        idx = next((i for i, (_, e) in enumerate(eps)
                    if e is not None and _ep_key(e) == cur), -1)
        ordered = eps[idx + 1:] + eps[:idx + 1] if idx >= 0 else eps
        with _FO_LOCK:
            tried = set(self.fo.get("tried", [])) | {cur}
        return [(r, e) for r, e in ordered if e is not None and _ep_key(e) not in tried]

    def stall_cap(self) -> float:
        """The stream bound while a next model exists; 0 = do not cap (also
        on the one extra send a live server earned: ordinary rules there)."""
        if not self.on or self.alive_waits or not self.candidates():
            return 0.0
        from aiforge_core.config import chat_failover
        return float(chat_failover.stall_s())

    def take(self, exc: BaseException, label: str, down: bool = False) -> bool:
        """Should the same request go again, now to ``self.ep``? True when the
        guard moved it (the user's switch, or a failover)."""
        if isinstance(exc, ModelSwitched) or (
                isinstance(exc, OSError) and self.abort()):
            sw = model_switch.latest(self.sid)
            return bool(sw) and self._user_switch(sw)
        if not self.on:
            return False
        stall = _stall_of(exc)
        if stall is not None:
            if stall.phase == "first_token" and not stall.keepalive \
                    and self.alive_waits == 0 and _server_alive(self.ep):
                # The server answers: a slow prefill, not a dead model. One
                # more send under the ordinary rules (its bounds doubled); a
                # second silent send fails over all the same.
                self.alive_waits += 1
                _log("llm.failover_held role=%s model=%s — server alive, "
                     "waiting once more", self.role, self.ep.model)
                return False
            what = ("sent only keep-alives" if stall.keepalive
                    else "no first token" if stall.phase == "first_token"
                    else "no new tokens")
            return self._fail_over(f"{what} for {_fmt(stall.seconds)}")
        if label == "timeout":
            # A read timeout already waited the caller's whole timeout (and
            # is never re-sent to the same model): as final as a stall.
            return self._fail_over("no answer within the read timeout")
        if label not in _FAIL_LABELS and not down:
            return False
        self.fails += 1
        from aiforge_core.config import chat_failover
        if down:
            return self._fail_over("its endpoint is unreachable")
        if self.fails >= chat_failover.fails():
            return self._fail_over(f"{self.fails} failed sends in a row ({label})")
        return False

    def _fail_over(self, why: str) -> bool:
        cands = self.candidates()
        if not cands:
            return False
        row, ep = cands[0]
        old = self.ep.model
        with _FO_LOCK:
            tried = self.fo.setdefault("tried", [])
            if _ep_key(self.ep) not in tried:
                tried.append(_ep_key(self.ep))
            self.fo["to"] = dict(row)
        self._move_to(ep)
        _log("llm.failover role=%s from=%s to=%s why=%s", self.role, old, ep.model, why)
        _notify(f"⚠ model {old} not responding ({why}) — switched to {ep.model}; "
                f"staying on it for the rest of this turn", "failover")
        _phase(self.sid, ep.model)
        return True

    @contextlib.contextmanager
    def bound(self):
        """This thread's sends belong to this guard (stream cap, abort)."""
        tok = _CURRENT.set(self)
        try:
            yield self
        finally:
            _CURRENT.reset(tok)

    def close(self) -> None:
        model_switch.leave(self.sid)


def begin(ep, payload: bytes, role: str, source: str = "") -> "Guard | None":
    """The guard for one POST, or None outside a chat (no session bound):
    then nothing here applies and the POST behaves exactly as before."""
    sid = _session_id()
    if not sid:
        return None
    try:
        from aiforge_core.llm import request_health
        return Guard(ep, payload, role, source, sid, request_health.turn_state())
    except Exception:  # noqa: BLE001 — failover must never break a call
        return None


def sleep_or_switch(seconds: float, guard: "Guard | None", sleep) -> bool:
    """Sleep ``seconds`` (a retry backoff) in short slices, ending early when
    the user picks another model meanwhile. True = a switch is waiting: the
    caller re-sends now instead of after the backoff."""
    if guard is None or not (guard.on or guard.abort()):
        sleep(seconds)          # nothing to cut it short for: one plain sleep
        return False
    left = max(0.0, float(seconds))
    while left > 0:
        if guard.abort():
            return True
        step = min(0.1, left)
        sleep(step)
        left -= step
    return guard.abort()


def switch_pending(session_id, since_seq: int) -> bool:
    """Has the chat's model been switched since ``since_seq`` (for a wait
    outside any request, llm/model_wait)?"""
    return session_id not in (None, "") and model_switch.seq_now(session_id) > since_seq


def sticky_endpoint(ep):
    """``ep`` as the running turn now sends it: the turn's failover target
    when ``ep`` is a model the turn failed over from, else ``ep``. For the
    outage wait (client._waiting), which must probe the model the request
    really goes to, not the one the role resolves to."""
    try:
        from aiforge_core.llm import request_health
        fo = (request_health.turn_state() or {}).get("failover") or {}
        with _FO_LOCK:
            to, tried = fo.get("to"), list(fo.get("tried") or [])
        if not to or _ep_key(ep) not in tried:
            return ep
        from aiforge_core.llm.client._chain import _chain_endpoint
        alt = _chain_endpoint(to, ep)
        return alt if alt is not None and _ep_key(alt) not in tried else ep
    except Exception:  # noqa: BLE001
        return ep


def current() -> "Guard | None":
    return _CURRENT.get()


def stall_cap() -> float:
    """The failover cap on this thread's stream bounds (0 = none)."""
    g = _CURRENT.get()
    try:
        return g.stall_cap() if g is not None else 0.0
    except Exception:  # noqa: BLE001
        return 0.0


def abort_check():
    """A callable the POST's watcher polls (True = cut the send for a model
    switch), or None when no guard is bound."""
    g = _CURRENT.get()
    return g.abort if g is not None else None


__all__ = ["Guard", "ModelSwitched", "abort_check", "begin", "current",
           "sleep_or_switch", "stall_cap", "sticky_endpoint"]
