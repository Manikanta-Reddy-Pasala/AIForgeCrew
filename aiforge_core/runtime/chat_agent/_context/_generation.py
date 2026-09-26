from __future__ import annotations

import os


# Loop detection: no fixed step budget — long coding sessions run until
# the agent finishes. We stop only when it's clearly STUCK: the same
# tool+args repeated this many times, or identical model output N times
# in a row. ``_SAFETY_CAP`` is a last-resort runaway guard (very high;
# tune in Settings → Agent limits, or AIFORGE_CHAT_SAFETY_CAP), not a normal
# stopping point. A turn still making progress extends it — see _limits.py.
_LOOP_REPEAT = 4
_OUTPUT_REPEAT = 3


def _stuck_recovery_max() -> int:
    """How many times a stuck-loop trip (same action / identical output) is met
    with a progress-recap NUDGE before the run finally gives up. Local models on
    long tool chains lose track and re-issue an action they already ran (esp.
    re-reading a file) — a recap of what's done + 'do the NEXT step' recovers
    them, where a hard bail lost all the work. 0 restores the old hard-abort.
    Tune with AIFORGE_CHAT_STUCK_RECOVERIES (default 3)."""
    try:
        return max(0, int(os.environ.get("AIFORGE_CHAT_STUCK_RECOVERIES", "3")))
    except ValueError:
        return 3


import re as _re

# An assistant turn's synthesized action line ("ACTION: file_read"). Native tool
# calls and the text protocol both render to this, so one regex covers both.
_RECAP_ACTION_RE = _re.compile(r"^\s*ACTION:\s*(\w+)", _re.MULTILINE)
_RECAP_PATH_RE = _re.compile(r'"(?:path|file|filename|target)"\s*:\s*"([^"]+)"')


def _fold_recap_action(txt: str, files: list, seen: set, tallies: dict) -> None:
    """Fold one assistant message's ACTION line into the recap accumulators: a
    file-reading action contributes a de-duped basename, every other tool a
    name×count tally."""
    am = _RECAP_ACTION_RE.search(txt)
    if not am:
        return
    pm = _RECAP_PATH_RE.search(txt)
    if pm:
        base = pm.group(1).rstrip("/").rsplit("/", 1)[-1]
        if base and base not in seen:
            seen.add(base)
            files.append(base)
    else:
        tallies[am.group(1)] = tallies.get(am.group(1), 0) + 1


def _progress_recap(convo: list, *, max_files: int = 15) -> str:
    """Compact recap of the DISTINCT actions already taken, so a stuck model can
    see its own progress and pick the NEXT step instead of repeating a done one.

    File-reading actions → a de-duped basename list ('read: A.java, B.java …');
    every other tool → a name×count tally. Pure function of ``convo``,
    dependency-free, best-effort — '' when there's nothing to recap."""
    files: list[str] = []
    seen: set[str] = set()
    tallies: dict[str, int] = {}
    for m in convo:
        if isinstance(m, dict) and m.get("role") == "assistant" \
                and isinstance(m.get("content"), str):
            _fold_recap_action(m["content"], files, seen, tallies)
    parts: list[str] = []
    if files:
        shown = files[:max_files]
        more = f" (+{len(files) - len(shown)} more)" if len(files) > len(shown) else ""
        parts.append(f"Files already read ({len(files)}): "
                     + ", ".join(shown) + more)
    tally = ", ".join(f"{k}×{v}" for k, v in tallies.items())
    if tally:
        parts.append(f"Other actions run: {tally}")
    return " ".join(parts)


_CANCELLED = object()   # sentinel: generation abandoned because Stop was pressed

# Bound on concurrent generation threads (live + abandoned-but-still-running).
# H1 abandons a cancelled LLM call to a daemon thread; the underlying urllib
# request can't be interrupted, so it keeps a connection until it returns/times
# out (AIFORGE_LLM_TIMEOUT_S). This semaphore stops spam Stop+resend from
# stacking UNBOUNDED zombie generations: a new one waits for a slot (i.e. for a
# zombie to finish) — which matches reality on a serialized local backend. The
# wait itself is cancellable.
_GEN_SEM = None


def _gen_sem():
    global _GEN_SEM
    if _GEN_SEM is None:
        try:
            _n = max(1, int(os.environ.get("AIFORGE_CHAT_MAX_INFLIGHT_GEN", "3")))
        except ValueError:
            _n = 3
        _GEN_SEM = __import__("threading").BoundedSemaphore(_n)
    return _GEN_SEM


def _complete_cancellable(complete_fn, role, convo, session_id):
    """Run the (synchronous, uncancellable) LLM call on a side thread so a Stop
    can interrupt it. H1: previously the cancel flag was only checked between
    ReAct steps, so on a slow local model Stop appeared dead for the WHOLE
    generation (minutes). Now we poll the cancel token while the call runs and
    return the ``_CANCELLED`` sentinel the instant it's set — abandoning the
    call (it finishes in the background, daemon thread, result ignored). The
    sentinel (not ``None``) keeps a legitimately-empty completion distinct from
    a cancel. No session → call inline. See :func:`_complete_live` for the
    streaming form the chat step uses."""
    gen = _complete_live(complete_fn, role, convo, session_id, stream=False)
    while True:
        try:
            next(gen)
        except StopIteration as stop:
            return stop.value


def _stream_enabled() -> bool:
    return os.environ.get("AIFORGE_CHAT_STREAM", "1").strip().lower() not in (
        "0", "false", "no", "off")


_QUEUED_TEXT = ("⏳ queued — every generation slot is in use by other chats; "
                "this one starts as soon as a slot frees (Stop ends it)")
_QUEUED_AFTER_S = 0.5


def _acquire_slot(sem, session_id):
    """A generation slot, waited for cancellably. At the cap, a fresh
    generation blocks until a prior (possibly abandoned) one finishes — and
    says so once (a "queued" thought) instead of sitting there silently.
    Returns True when held, False on Stop."""
    import time as _t
    from aiforge_core.runtime import chat_cancel
    t0, told = _t.monotonic(), False
    while not sem.acquire(timeout=0.2):
        if chat_cancel.is_cancelled(session_id):
            return False
        if not told and _t.monotonic() - t0 >= _QUEUED_AFTER_S:
            told = True
            yield {"type": "thought", "role": "system", "text": _QUEUED_TEXT,
                   "llm_wait": {"state": "queued"}}
    return True


def _slot_hooks(sem, box, ev, waits):
    """While the call WAITS for a model that is down (llm/model_wait) it does
    not hold a generation slot: three chats waiting on a dead local endpoint
    must not block every other chat, healthy cloud roles included. The slot is
    taken again once the model is back (queued, cancellably, if all are
    busy)."""
    def _on_wait():
        if box.get("held"):
            box["held"] = False
            sem.release()

    def _on_back():
        import time as _t
        from aiforge_core.llm import model_wait
        t0, told = _t.monotonic(), False
        while not box.get("held"):
            if sem.acquire(timeout=0.2):
                box["held"] = True
                return
            if ev.is_set() or model_wait.cancel_reason():
                return           # the re-sent call aborts on the same cancel
            if not told and waits is not None \
                    and _t.monotonic() - t0 >= _QUEUED_AFTER_S:
                told = True
                waits.put({"text": _QUEUED_TEXT, "state": "queued"})
    return _on_wait, _on_back


def _start_call(complete_fn, role, convo, sem, deltas, waits=None):
    """Start the call on a daemon thread. Returns (thread, result box, the
    per-call abort event for the client HTTP layer). ``waits`` receives the
    status of any wait for the model inside the call (llm/model_wait)."""
    import threading as _th
    box: dict = {}
    ev = _th.Event()
    # A new thread starts with an EMPTY context, so the request context the
    # turn bound (session id, role) would be invisible to the LLM client —
    # which is what attributes a request to this chat in the call meter (and
    # the Langfuse session trace). Carry it over explicitly.
    import contextvars as _cv
    _ctx = _cv.copy_context()

    box["held"] = True           # the caller acquired the slot for us

    def _call():
        # The role this generation runs as — so the meter's by_role breakdown
        # ("which agent is burning the calls") is not permanently empty.
        try:
            from aiforge_core.runtime import request_context as _rc
            _rc.set_role(role)
        except Exception:  # noqa: BLE001
            pass
        # Bind the cancel token on THIS thread so the LLM client's HTTP layer
        # aborts the in-flight request the instant Stop fires; and, when the
        # caller streams, the sink that receives each token as it arrives.
        try:
            from aiforge_core.llm import client as _client
            _client.set_cancel_event(ev)
            if deltas is not None:
                _client.set_delta_sink(lambda kind, text: deltas.put((kind, text)))
            from aiforge_core.llm import model_wait
            if waits is not None:
                model_wait.bind_status_sink(waits.put)
            model_wait.bind_wait_hooks(*_slot_hooks(sem, box, ev, waits))
        except Exception:  # noqa: BLE001
            pass
        try:
            box["out"] = complete_fn(role, convo)
        except Exception as exc:  # noqa: BLE001 — surfaced on the main thread
            box["err"] = exc
        finally:
            if box.pop("held", False):
                sem.release()    # free the slot when the call REALLY finishes

    t = _th.Thread(target=lambda: _ctx.run(_call), daemon=True)
    t.start()
    return t, box, ev


def _complete_live(complete_fn, role, convo, session_id, stream: bool = True):
    """:func:`_complete_cancellable` that also YIELDS the answer as the model
    writes it: ``{"type": "delta", "phase": ...}`` events (see
    :class:`_DeltaShaper`), batched every ~60 ms. The whole answer used to
    arrive in one piece when the call finished. Returns the completion (or
    ``_CANCELLED``) like the plain form."""
    from aiforge_core.runtime import chat_cancel
    if session_id is None:
        return complete_fn(role, convo)
    sem = _gen_sem()
    if not (yield from _acquire_slot(sem, session_id)):
        return _CANCELLED
    import queue as _q
    deltas = _q.SimpleQueue() if stream and _stream_enabled() else None
    waits = _q.SimpleQueue()
    t, box, ev = _start_call(complete_fn, role, convo, sem, deltas, waits)
    shaper = _DeltaShaper() if deltas is not None else None
    while t.is_alive():
        if chat_cancel.is_cancelled(session_id):
            ev.set()             # abort the in-flight HTTP request
            return _CANCELLED    # slot frees when the (now-aborting) request ends
        t.join(timeout=0.06 if shaper else 0.2)
        if shaper:
            yield from shaper.drain(deltas)
        yield from _drain_waits(waits)
    if shaper:
        yield from shaper.drain(deltas)
    yield from _drain_waits(waits)
    # The request may have been aborted just as it finished — treat any
    # post-loop cancel as a cancel, not an error.
    if chat_cancel.is_cancelled(session_id):
        return _CANCELLED
    if "err" in box:
        raise box["err"]
    return box.get("out")


def _drain_waits(q) -> list:
    """The model-wait status lines queued by the call thread, as thoughts."""
    import queue as _q
    out: list = []
    while True:
        try:
            st = q.get_nowait()
        except _q.Empty:
            return out
        if isinstance(st, dict) and st.get("text"):
            out.append({"type": "thought", "role": "system", "text": st["text"],
                        "llm_wait": {k: st.get(k) for k in
                                     ("state", "url", "down_s", "next_probe_s")}})


# What the user should SEE of a completion while it is written. The text
# protocol writes "THOUGHT: … ACTION: … ARGS_JSON: …" for a tool step and
# "FINAL: <answer>" / "ASK: <question>" to finish; a native-FC model writes the
# answer as plain content. Only the answer part streams into the reply; the
# rest is a muted draft line, and a model's reasoning a "thinking" line.
_ANSWER_MARK_RE = _re.compile(r"^[ \t]*(?:FINAL|ASK):[ \t]*", _re.MULTILINE)
_PROTOCOL_HEADS = ("THOUGHT:", "ACTION:", "ARGS_JSON:")


class _DeltaShaper:
    def __init__(self) -> None:
        self.buf = ""
        self.mode = ""           # "", "plain" or "final": which answer was sent
        self.sent = 0            # chars of that answer already sent
        self.started = False

    def _answer(self) -> "tuple[str, str]":
        """(kind, answer text so far): kind "draft" = nothing to show yet."""
        b = self.buf
        head = b.lstrip()
        if head.startswith("<think>"):
            if "</think>" not in head:
                return "draft", ""
            b = head.split("</think>", 1)[1]
        marks = list(_ANSWER_MARK_RE.finditer(b))
        last = marks[-1] if marks else None
        if last is not None:
            return "final", b[last.end():]
        head = b.lstrip()
        if not head or any(p.startswith(head) or head.startswith(p)
                           for p in _PROTOCOL_HEADS):
            return "draft", ""
        return "plain", head

    def _shape(self, chunk: str) -> list:
        self.buf += chunk
        kind, text = self._answer()
        if kind == "draft":
            return [{"type": "delta", "phase": "draft", "text": chunk}]
        out: list = []
        if self.mode and kind != self.mode:     # a plain start turned into FINAL:
            out.append({"type": "delta", "phase": "reset"})
            self.sent = 0
        self.mode = kind
        new, self.sent = text[self.sent:], len(text)
        if new:
            out.append({"type": "delta", "phase": "answer", "text": new})
        return out

    def _restart(self) -> list:
        """A new model call (a retry) begins: forget the previous text."""
        had = self.started
        self.buf, self.mode, self.sent, self.started = "", "", 0, True
        return [{"type": "delta", "phase": "reset"}] if had else []

    def _emit_run(self, kind: str, parts: list) -> list:
        if not parts:
            return []
        if kind == "reasoning":
            return [{"type": "delta", "phase": "thinking", "text": "".join(parts)}]
        return self._shape("".join(parts))

    def drain(self, q) -> list:
        """Everything queued since the last drain, in arrival order, with runs
        of the same kind merged into one event."""
        import queue as _q
        items: list = []
        while True:
            try:
                items.append(q.get_nowait())
            except _q.Empty:
                break
        out: list = []
        if items and not self.started:
            self.started = True
            out.append({"type": "delta", "phase": "reset"})
        run_kind, run = "", []
        for kind, text in items:
            if kind != run_kind:
                out += self._emit_run(run_kind, run)
                run_kind, run = kind, []
            if kind == "start":
                out += self._restart()
            else:
                run.append(text)
        out += self._emit_run(run_kind, run)
        return out
