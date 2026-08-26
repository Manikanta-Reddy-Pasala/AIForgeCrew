"""Observability routes — split out of api.py (APIRouter).

The service health probe, the live log/trace SSE tails (role log tail,
per-ticket graph-runner trace, per-ticket LLM-call trace + its non-streaming
listing), and the workflow-topology DAG snapshot + SSE refresh. Handlers keep
their inline function-local imports and behaviour VERBATIM.
"""
from __future__ import annotations

import asyncio
import json
import os
import re

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from aiforge_core.api.routes._sse import sse_response

from aiforge_core.config.env import LM_STUDIO_BASE_URL, LOG_DIR
from aiforge_core.tickets import store as tickets_mod

_AIFORGE_LOGS_GRAPH_RUNNER_ER = '~/.aiforge/logs/graph-runner.err'

router = APIRouter()


# ─────────────────────────── Health ─────────────────────────────────────
@router.get("/api/health")
def health() -> dict:
    from aiforge_core.tickets.backend_factory import get_backend
    status = {"ok": True, "storage": None, "lm_studio": False}
    try:
        be = get_backend()
        status["storage"] = be.name
        # Cheap reachability probe — an identifier that never exists.
        tickets_mod.get("__healthcheck__")
    except Exception:
        status["ok"] = False
    try:
        import urllib.request

        from aiforge_core.net.ssl import context_for as _ssl_context_for
        _lm_url = f"{LM_STUDIO_BASE_URL}/models"
        with urllib.request.urlopen(
            _lm_url, timeout=3, context=_ssl_context_for(_lm_url)) as r:
            status["lm_studio"] = r.getcode() == 200
    except Exception:
        pass
    # Langfuse trace mirror (optional): expose ONLY the host so the UI can
    # render a "Traces ↗" link — never the keys.
    try:
        from aiforge_core.integrations import langfuse_adapter as _lfa
        if _lfa.enabled():
            status["traces_url"] = os.environ.get("LANGFUSE_HOST", "")
    except Exception:  # noqa: BLE001
        pass
    return status


# ─────────────────────────── Logs SSE ───────────────────────────────────
# Recognised log files (newest naming first). When no orchestrator-<role>
# exists, fall back to the ADK-prefixed file (current convention) and
# finally the master adk_runner stream so the UI never tails an empty
# legacy file.
def _resolve_role_log(role: str) -> str:
    candidates = [
        os.path.join(LOG_DIR, f"orchestrator-adk.{role}.ndjson"),
        os.path.join(LOG_DIR, f"orchestrator-{role}.ndjson"),
        os.path.join(LOG_DIR, "orchestrator-adk_runner.ndjson"),
    ]
    for p in candidates:
        if os.path.exists(p) and os.path.getsize(p) > 0:
            return p
    return candidates[0]   # let the tailer wait for the primary to appear


_EXTRA_LOG_ROLES = {"intent", "publish", "integration", "adk_runner",
                    "enhancer", "architect", "verifier"}


# ────────────────────── LLM request meter ───────────────────────────────
@router.get("/api/llm/usage")
def llm_usage(series: bool = True) -> dict:
    """Machine-wide LLM request meter — what the toolbar badge shows.

    Every HTTP attempt this process has sent to a model, whoever asked for it:
    chat turns, the pipeline, jobs, and the background memory daemon (whose
    folds and scope calls are invisible in the chat footer yet are exactly what
    makes an interactive turn feel slow).

    ``per_minute`` is an exact sliding 60s window. ``last_15m`` / ``last_60m``
    are rolling too, but MINUTE-ALIGNED (they sum whole-minute buckets, so
    "last 15 min" covers 14–15 minutes) — the trade that keeps a read O(60)
    instead of a scan of the hour under the same lock every LLM call takes.
    ``series_60m`` is one count per minute for the last hour, oldest first, for
    the sparkline; ``by_model`` is the axis that actually distinguishes
    endpoints when one provider class serves all of them. In-process and reset
    on restart — a live meter, not billing.

    FAILURES are reported alongside every one of those windows
    (``failed_per_minute`` / ``failed_15m`` / ``failed_60m`` / ``failed``,
    ``by_fail_reason``, ``series_fail_60m``) and are a SUBSET of them, never a
    separate population: a request that failed still went out, still cost the
    endpoint and still counted against its rate limit, so subtracting failures
    from the rate would make the meter read its quietest exactly when the box
    is drowning in retries. Each failure is charged to the minute of its SEND,
    so a 600s timeout lands in the minute whose traffic it actually was.
    """
    from aiforge_core.llm import call_meter
    return call_meter.global_snapshot(series=bool(series))


async def _tail_lines(path: str, n: int = 200) -> list[str]:
    """The last ``n`` lines, read OFF the event loop — an append-only log can be
    large, and a blocking read here stalls EVERY other request the server is
    serving, not just this stream. deque(maxlen) holds only the tail instead of
    materialising the whole (unbounded) file."""
    import collections as _coll

    def _sync():
        with open(path, encoding="utf-8") as f:
            return list(_coll.deque(f, maxlen=n))
    return await asyncio.to_thread(_sync)


async def _read_from(path: str, offset: int) -> str:
    def _sync():
        with open(path, encoding="utf-8") as f:
            f.seek(offset)
            return f.read()
    return await asyncio.to_thread(_sync)


def _sse_lines(text: str):
    for line in text.splitlines():
        line = line.strip()
        if line:
            yield f"data: {line}\n\n"


async def _backfill(path: str):
    """Yield the recent history on connect so the page shows something
    immediately instead of a blank "waiting for events…". Returns nothing; the
    caller re-reads the size."""
    for line in await _tail_lines(path):
        line = line.strip()
        if line:
            yield f"data: {line}\n\n"


def _size_of(path: str) -> int:
    return os.path.getsize(path) if os.path.exists(path) else 0


async def _poll_appends(path: str, last_size: int):
    """Yield each new line as the file grows."""
    while True:
        await asyncio.sleep(1.5)
        size = _size_of(path)
        if size <= last_size:
            continue
        chunk = await _read_from(path, last_size)
        last_size = size
        for event in _sse_lines(chunk):
            yield event


async def _tail_forever(path: str):
    """Backfill the recent history, then poll for appends until cancelled."""
    last_size = 0
    if os.path.exists(path):
        try:
            async for chunk in _backfill(path):
                yield chunk
        except Exception:  # noqa: BLE001
            pass
        last_size = _size_of(path)
    # CancelledError propagates (not swallowed) so shutdown/disconnect handling
    # knows the stream was actually cancelled.
    async for event in _poll_appends(path, last_size):
        yield event


@router.get("/api/logs/{role}/stream")
def stream_role_log(role: str):
    # Accept any role (sanitised) — an unknown role just tails an empty file
    # rather than 404-ing the tab. Prevents path traversal.
    role = re.sub(r"[^a-z0-9_]", "", (role or "").lower()) or "adk_runner"
    return sse_response(_tail_forever(_resolve_role_log(role)))


# ─────────────────────────── Ticket trace SSE ───────────────────────────
#
# Live tail of the graph-runner master log, filtered by ticket identifier.
# The UI /trace/:id view subscribes and renders Step/Action/Observation as
# it arrives so ops can watch a run in progress and decide whether to
# intervene (cancel ticket, swap model, add hint).


# Scope management via structured NDJSON events. Both legacy (graph_runner.*)
# and current (adk_runner.*) event names are accepted so older + newer runs
# both stream cleanly.
_TRACE_START_MARKERS = ('"event": "graph_runner.start"',
                        '"event":"graph_runner.start"',
                        '"event": "adk_runner.start"',
                        '"event":"adk_runner.start"')
_TRACE_DONE_MARKERS = ('"event": "graph_runner.done"',
                       '"event":"graph_runner.done"',
                       '"event": "adk_runner.done"',
                       '"event":"adk_runner.done"')


async def _tail_proc(path: str, host: str, lines: int = 500):
    """A ``tail -F`` on ``path``, over ssh when a remote host is configured.

    Local unless AIFORGE_GRAPH_RUNNER_HOST is set — the api now runs on the
    same host as the graph-runner, so ssh-to-self was the previous bug.
    """
    argv = (["ssh", "-o", "ConnectTimeout=5", host, f"tail -Fn{lines} {path}"]
            if host else ["tail", f"-Fn{lines}", path])
    return await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL)


async def _pump_into(queue, path: str, host: str) -> None:
    """Feed every line of ``path`` into ``queue``; None marks this tail's end."""
    proc = await _tail_proc(path, host)
    try:
        while True:
            line = await proc.stdout.readline()
            if not line:
                await asyncio.sleep(0.3)
                continue
            await queue.put(line.decode("utf-8", "replace").rstrip("\n"))
    finally:
        try:
            proc.kill()
            await proc.wait()   # reap — don't leak a zombie
        except Exception:  # noqa: BLE001
            pass
        await queue.put(None)


def _trace_scope(raw: str, identifier: str, in_ctx: bool) -> tuple[bool, bool]:
    """``(in_ctx, emit)`` for one raw line, tracking this ticket's run window."""
    quoted = f'"{identifier}"'
    if any(m in raw for m in _TRACE_START_MARKERS):
        return (quoted in raw), False
    if any(m in raw for m in _TRACE_DONE_MARKERS) and quoted in raw:
        return False, True          # emit the closing line, then leave scope
    return in_ctx, in_ctx


async def _merged_trace(log: str, err: str, host: str, identifier: str):
    """One tail per file, interleaved via a queue so either stream can deliver
    a line as soon as it arrives."""
    queue: asyncio.Queue = asyncio.Queue()
    tasks = [asyncio.create_task(_pump_into(queue, log, host)),
             asyncio.create_task(_pump_into(queue, err, host))]
    in_ctx = False
    try:
        while True:
            raw = await queue.get()
            if raw is None:
                return
            in_ctx, emit = _trace_scope(raw, identifier, in_ctx)
            if emit:
                yield f"data: {json.dumps({'line': raw})}\n\n"
    finally:
        for t in tasks:
            t.cancel()


@router.get("/api/trace/{identifier}/stream")
def stream_ticket_trace(identifier: str):
    """Tail graph-runner logs on the orchestrator host + stream lines for
    this ticket. Merges the smolagents stdout stream (``graph-runner.log``)
    with the structured NDJSON stream (``graph-runner.err``) so the client
    sees Step/Action/Observation AND ``llm.call`` / agent ndjson events
    (including raw prompt + completion) interleaved in time order.
    """
    host = os.environ.get("AIFORGE_GRAPH_RUNNER_HOST", "").strip()
    log = os.environ.get(
        "AIFORGE_GRAPH_RUNNER_LOG",
        os.path.expanduser("~/.aiforge/logs/graph-runner.log"))
    err = os.environ.get(
        "AIFORGE_GRAPH_RUNNER_ERR",
        os.path.expanduser(_AIFORGE_LOGS_GRAPH_RUNNER_ER))
    return sse_response(_merged_trace(log, err, host, identifier))


# ─────────────────────────── LLM call trace ─────────────────────────────
#
# Per-ticket stream of just the ``llm.call`` NDJSON events — full chat
# messages sent to the model + full response content + token usage +
# wall time. Use this when you want to see exactly what each Planner /
# Doer tick said to the LLM and what came back, without the smolagents
# stdout noise.


def _is_llm_call_for(raw: str, identifier: str) -> bool:
    """An ``llm.call`` NDJSON line for this ticket, in either JSON spacing."""
    return (('"event": "llm.call"' in raw or '"event":"llm.call"' in raw)
            and (f'"ticket": "{identifier}"' in raw
                 or f'"ticket":"{identifier}"' in raw))


async def _llm_trace_lines(err: str, host: str, identifier: str):
    proc = await _tail_proc(err, host, lines=2000)
    try:
        while True:
            line = await proc.stdout.readline()
            if not line:
                await asyncio.sleep(0.3)
                continue
            raw = line.decode("utf-8", "replace").rstrip("\n")
            if _is_llm_call_for(raw, identifier):
                yield f"data: {raw}\n\n"
    finally:
        try:
            proc.kill()
            await proc.wait()   # reap — don't leak a zombie
        except Exception:  # noqa: BLE001
            pass


@router.get("/api/llm-trace/{identifier}/stream")
def stream_llm_trace(identifier: str):
    err = os.environ.get(
        "AIFORGE_GRAPH_RUNNER_ERR",
        os.path.expanduser(_AIFORGE_LOGS_GRAPH_RUNNER_ER))
    host = os.environ.get("AIFORGE_GRAPH_RUNNER_HOST", "").strip()
    return sse_response(_llm_trace_lines(err, host, identifier))


@router.get("/api/llm-trace/{identifier}")
def list_llm_trace(identifier: str, limit: int = 50):
    """Non-streaming: return the last N ``llm.call`` events for this ticket
    as a JSON list. Easier to inspect in a browser / curl | jq."""
    err = os.environ.get(
        "AIFORGE_GRAPH_RUNNER_ERR",
        os.path.expanduser(_AIFORGE_LOGS_GRAPH_RUNNER_ER),
    )
    needle_event = '"event": "llm.call"'
    needle_event_compact = '"event":"llm.call"'
    needle_ticket = f'"ticket": "{identifier}"'
    needle_ticket_compact = f'"ticket":"{identifier}"'
    events: list[dict] = []
    try:
        with open(err, encoding="utf-8", errors="replace") as f:
            for line in f:
                if (needle_event in line or needle_event_compact in line) and \
                   (needle_ticket in line or needle_ticket_compact in line):
                    try:
                        events.append(json.loads(line))
                    except Exception:
                        continue
    except FileNotFoundError:
        return {"events": [], "count": 0, "error": f"{err} not found"}
    events = events[-limit:]
    return {"events": events, "count": len(events)}


# ─────────────────────────── Workflow topology (DAG view) ──────────────
def _static_topology() -> dict:
    """Static v6 pipeline DAG — fallback when no live topology module is
    present, so the Workflow view renders instead of erroring."""
    # Mirror the live v6 pipeline order (runtime.workflow_topology). Linear
    # projection of the real DAG: triage → enhancer → context/research →
    # planner → verifier → doer loop → validator → learner.
    stages = ["triage", "enhancer", "researcher", "planner", "verifier",
              "doer", "refiner", "feedback", "validator", "learner"]
    nodes = [{"id": s, "label": s, "type": "agent", "tools": [],
              "status": "idle", "last_event_at": None,
              "skills": [], "rules": [], "workflows": []} for s in stages]
    edges = [{"from": stages[i], "to": stages[i + 1], "label": ""}
             for i in range(len(stages) - 1)]
    return {"nodes": nodes, "edges": edges, "ticket": None, "static": True,
            "context": {"skills": [], "rules": [], "workflows": []}}


def _topology_snapshot(ticket: str | None) -> dict:
    try:
        from aiforge_core.runtime import workflow_topology as _wt
        return _wt.snapshot(ticket)
    except Exception:
        return _static_topology()


@router.get("/api/workflow/topology")
def workflow_topology(ticket: str | None = None) -> dict:
    """DAG snapshot for the UI graph view. Optional ?ticket=X overlays
    per-node status + last_event_at. Falls back to a static pipeline DAG
    when no live topology module is available."""
    return _topology_snapshot(ticket)


@router.get("/api/workflow/stream")
def workflow_stream(ticket: str | None = None,
                    interval: int = 3) -> StreamingResponse:
    """SSE topology refresh. Emits one snapshot every ``interval``
    seconds (clamped 1..30). UI ``EventSource`` consumes for live
    DAG status. Disconnect-safe — generator exits when client closes.
    """
    interval = max(1, min(int(interval or 3), 30))

    def _gen():
        import time as _t
        while True:
            snap = _topology_snapshot(ticket)
            yield f"data: {json.dumps(snap)}\n\n"
            _t.sleep(interval)

    return sse_response(_gen())
