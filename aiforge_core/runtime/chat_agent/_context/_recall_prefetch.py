"""Session-start memory recall, started while the enhancer's LLM call runs.

The first turn used to run two recalls back to back: the enhancer's own, then
— after the enhancer's LLM call — the context bundle's. The bundle's recall
does not depend on the enhancer's output (it keys on the user's raw words,
cut at the "[Interpreted request" marker), and most of its cost is the
reranker sidecar, not the main model. So it is started in the background the
moment the enhancer hands its prompt to the LLM, and the bundle picks the
result up instead of querying again.

The bundle only ever uses a prefetched result when the call it would make is
IDENTICAL (query, limit, repo, session, boost tags) — anything else, or any
error, and it runs its own query exactly as before. It always waits for an
in-flight prefetch first: the reranker sidecar answers two concurrent
requests with a 500 for one of them, which would silently skip reranking.
"""
from __future__ import annotations

import contextvars
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="recall-prefetch")
_PENDING: dict = {}          # session_id -> (args, future, started_monotonic)
_LOCK = threading.Lock()
_INTERPRETED_MARK = "\n\n---\n[Interpreted request"


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _predicted_args(messages, cwd, session_id) -> tuple | None:
    """The recall call the bundle will make for these messages, or None when it
    won't recall. Mirrors ``_blocks._append_context_blocks`` (gating) and
    ``_convo._seed_prompt`` (the query). A wrong guess only costs a query."""
    from ._compaction import _text_of
    from ._recall import _recall_args
    from ._window import _cave_mode, _ctx_on
    proactive = os.environ.get(
        "AIFORGE_CHAT_PROACTIVE_RECALL", "lite").strip().lower()
    is_init = not any(m.get("role") == "assistant" for m in messages)
    if not ((proactive == "full" or is_init) and _ctx_on("recall")):
        return None
    last_user = next(
        (_text_of(m) for m in reversed(messages)
         if (m.get("role") or "user") == "user" and m.get("content")), "")
    q = (last_user.split(_INTERPRETED_MARK)[0].strip() or last_user).strip()
    if not q:
        return None
    return _recall_args(cwd, q, 3 if _cave_mode() else 6, session_id)


def start(messages, cwd, session_id) -> None:
    """Kick off the bundle's recall for this turn in the background. Carries
    the caller's context (request repo root, trace) into the worker. Off with
    AIFORGE_RECALL_PREFETCH=0. Never raises."""
    if session_id is None or os.environ.get("AIFORGE_RECALL_PREFETCH", "1") != "1":
        return
    try:
        args = _predicted_args(messages or [], cwd, session_id)
        if args is None:
            return
        from ._recall import _run_recall
        fut = _POOL.submit(contextvars.copy_context().run, _run_recall, args)
        with _LOCK:
            _PENDING[session_id] = (args, fut, time.monotonic())
    except Exception:  # noqa: BLE001 — a prefetch must never break a turn
        pass


def take(args: tuple) -> dict | None:
    """The prefetched result for exactly this recall call, else None (the
    caller then queries itself). Waits for an in-flight prefetch either way so
    the two never hit the reranker at the same time."""
    with _LOCK:
        entry = _PENDING.pop(args[3], None)
    if entry is None:
        return None
    want, fut, started = entry
    try:
        res = fut.result(timeout=_env_float("AIFORGE_RECALL_PREFETCH_WAIT_S", 30.0))
    except Exception:  # noqa: BLE001 — timeout or a failed query: query again
        return None
    # A leftover from a turn that never built its bundle must not stand in for
    # a later one's recall.
    if want != args or time.monotonic() - started > _env_float(
            "AIFORGE_RECALL_PREFETCH_MAX_AGE_S", 300.0):
        return None
    return res if isinstance(res, dict) else None
