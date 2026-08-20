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
_RECAP_ACTION_RE = _re.compile(r"^\s*ACTION:\s*([A-Za-z0-9_]+)", _re.MULTILINE)
_RECAP_PATH_RE = _re.compile(r'"(?:path|file|filename|target)"\s*:\s*"([^"]+)"')


def _progress_recap(convo: list, *, max_files: int = 15) -> str:
    """Compact recap of the DISTINCT actions already taken, so a stuck model can
    see its own progress and pick the NEXT step instead of repeating a done one.

    File-reading actions → a de-duped basename list ('read: A.java, B.java …');
    every other tool → a name×count tally. Pure function of ``convo`` (scans the
    assistant ACTION lines the loop appended), dependency-free, best-effort —
    returns '' when there's nothing to recap."""
    files: list[str] = []
    seen: set[str] = set()
    tallies: dict[str, int] = {}
    for m in convo:
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        txt = m.get("content")
        if not isinstance(txt, str):
            continue
        am = _RECAP_ACTION_RE.search(txt)
        if not am:
            continue
        tool = am.group(1)
        pm = _RECAP_PATH_RE.search(txt)
        if pm:
            base = pm.group(1).rstrip("/").rsplit("/", 1)[-1]
            if base and base not in seen:
                seen.add(base)
                files.append(base)
        else:
            tallies[tool] = tallies.get(tool, 0) + 1
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
    a cancel. No session → call inline."""
    from aiforge_core.runtime import chat_cancel
    if session_id is None:
        return complete_fn(role, convo)
    import threading as _th
    sem = _gen_sem()
    # Acquire a generation slot (cancellable wait). At the cap, a fresh
    # generation blocks until a prior (possibly abandoned) one finishes.
    while not sem.acquire(timeout=0.2):
        if chat_cancel.is_cancelled(session_id):
            return _CANCELLED

    box: dict = {}
    ev = _th.Event()             # per-call abort signal for the client HTTP layer

    def _call():
        # Bind the cancel token on THIS thread so the LLM client's HTTP layer
        # aborts the in-flight request the instant Stop fires (true model-
        # reclaim, not just abandoning the thread). Best-effort — a stub
        # complete_fn that never reaches the client is simply unaffected.
        try:
            from aiforge_core.llm import client as _client
            _client.set_cancel_event(ev)
        except Exception:  # noqa: BLE001
            pass
        try:
            box["out"] = complete_fn(role, convo)
        except Exception as exc:  # noqa: BLE001 — surfaced on the main thread
            box["err"] = exc
        finally:
            sem.release()        # free the slot when the call REALLY finishes

    t = _th.Thread(target=_call, daemon=True)
    t.start()
    while t.is_alive():
        if chat_cancel.is_cancelled(session_id):
            ev.set()             # abort the in-flight HTTP request
            return _CANCELLED    # slot frees when the (now-aborting) request ends
        t.join(timeout=0.2)
    # The request may have been aborted just as it finished — treat any
    # post-loop cancel as a cancel, not an error.
    if chat_cancel.is_cancelled(session_id):
        return _CANCELLED
    if "err" in box:
        raise box["err"]
    return box.get("out")
