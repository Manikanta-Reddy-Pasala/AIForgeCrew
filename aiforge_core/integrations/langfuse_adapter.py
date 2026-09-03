"""langfuse adapter — mirror LLM calls to a self-hosted Langfuse server.

SIMPLEST possible integration: NO SDK (the langfuse python SDK's pins
conflict with the app's own pins, same class as ragas) — we POST directly to the
public ingestion REST API (``/api/public/ingestion``, basic-auth pk/sk),
which both server v2 and v3 accept. httpx is already a core dependency, so
tracing needs NO extra install at all.

Purpose is narrow by operator requirement: SEE the agents' messages and the
LLM responses per call in the Langfuse UI. The existing file-based tracing
stays the source of truth.

Enable purely by env (run.sh --with-langfuse sets these automatically):
    LANGFUSE_HOST=http://127.0.0.1:3005
    LANGFUSE_PUBLIC_KEY=pk-lf-…
    LANGFUSE_SECRET_KEY=sk-lf-…
Optional: AIFORGE_LANGFUSE_DISABLE=1 kills it even with keys set;
AIFORGE_LANGFUSE_MAX_CHARS caps per-field payload size (default 8000).

Sends are fire-and-forget on a daemon thread — a slow/down Langfuse can
never block or break a turn.
"""
from __future__ import annotations

import datetime
import os
import threading
import uuid


def available() -> bool:
    return True          # raw REST over httpx (core dep) — always available


def enabled() -> bool:
    if str(os.environ.get("AIFORGE_LANGFUSE_DISABLE", "")).strip().lower() \
            in ("1", "true", "yes", "on"):
        return False
    return bool(os.environ.get("LANGFUSE_HOST")
                and os.environ.get("LANGFUSE_PUBLIC_KEY")
                and os.environ.get("LANGFUSE_SECRET_KEY"))


def _cap() -> int:
    try:
        return max(500, int(os.environ.get("AIFORGE_LANGFUSE_MAX_CHARS", "8000")))
    except ValueError:
        return 8000


def _send(payload: dict) -> None:
    """POST one ingestion batch. Runs on the fire-and-forget thread.

    Logs a WARNING when the ingestion API rejects events — the endpoint always
    returns 207 with a per-event ``errors`` list, so a silently-dropped
    generation (input/output never showing in the UI) becomes visible in the
    aiforge logs instead of a black box."""
    import logging

    import httpx
    host = (os.environ.get("LANGFUSE_HOST") or "").rstrip("/")
    # This posts whole PROMPTS to a remote host, so it is the highest-content
    # telemetry channel in the system — it answers to the egress policy like
    # any other declared destination.
    from aiforge_core.net import egress as _egress
    # Pass the full INGEST URL, not the bare host: urlsplit on "langfuse.corp"
    # yields hostname=None, so the check refused every send — telemetry off with
    # the host correctly allowlisted, which no operator could have debugged.
    _ingest = f"{host}/api/public/ingestion"
    if host and _egress.allow("telemetry", _ingest, method="POST") is not None:
        return
    log = logging.getLogger("aiforge.langfuse")
    try:
        r = httpx.post(f"{host}/api/public/ingestion", json=payload,
                       auth=(os.environ.get("LANGFUSE_PUBLIC_KEY", ""),
                             os.environ.get("LANGFUSE_SECRET_KEY", "")),
                       timeout=5)
    except Exception as exc:  # noqa: BLE001
        log.debug("langfuse ingest send failed: %s", exc)
        return
    if r.status_code >= 300 and r.status_code != 207:
        log.warning("langfuse ingest HTTP %s: %s", r.status_code, r.text[:500])
        return
    try:
        errs = (r.json() or {}).get("errors") or []
    except Exception:  # noqa: BLE001 — non-JSON body
        errs = []
    if errs:
        log.warning("langfuse ingest rejected %d event(s): %s",
                    len(errs), str(errs)[:500])


def record_generation(*, role: str, model: str = "", messages=None,
                      output: str = "", latency_ms: int = 0,
                      error: str = "", session_id=None,
                      metadata: dict | None = None) -> None:
    """One LLM completion → one Langfuse trace+generation, sent async.
    Raises only on payload-build errors — the caller wraps regardless."""
    cap = _cap()
    now = datetime.datetime.now(datetime.timezone.utc)
    start = now - datetime.timedelta(milliseconds=max(0, latency_ms))
    trace_id = str(uuid.uuid4())
    msgs = [{"role": m.get("role"), "content": str(m.get("content"))[:cap]}
            for m in (messages or []) if isinstance(m, dict)]
    meta = {**(metadata or {}), "role": role}
    if error:
        meta["error"] = error[:500]
    if session_id:
        meta["session_id"] = session_id
    out = (output or "")[:cap]
    # Mirror input/output onto the TRACE too — the Langfuse trace header and the
    # Sessions view surface trace-level input/output, so setting them only on the
    # nested generation left the trace/session showing null. Belt-and-suspenders:
    # the generation carries them as well for the observation detail.
    payload = {"batch": [
        {"id": str(uuid.uuid4()), "type": "trace-create",
         "timestamp": now.isoformat(),
         "body": {"id": trace_id, "name": f"llm:{role}",
                  "timestamp": start.isoformat(), "metadata": meta,
                  "input": msgs, "output": out,
                  **({"sessionId": str(session_id)} if session_id else {})}},
        {"id": str(uuid.uuid4()), "type": "generation-create",
         "timestamp": now.isoformat(),
         "body": {"id": str(uuid.uuid4()), "traceId": trace_id,
                  "name": f"llm:{role}",
                  "startTime": start.isoformat(), "endTime": now.isoformat(),
                  "input": msgs, "output": out, "metadata": meta,
                  **({"model": model} if model else {}),
                  **({"level": "ERROR", "statusMessage": error[:500]}
                     if error else {})}},
    ]}
    threading.Thread(target=_send, args=(payload,), daemon=True).start()


def record_score(*, name: str, value: float, session_id=None,
                 comment: str = "", data_type: str = "NUMERIC",
                 metadata: dict | None = None) -> None:
    """One evaluation score → a Langfuse trace+score, sent async.

    A score must reference a traceId, so we mint a tiny turn-level trace to
    carry it and tag that trace with ``sessionId`` — so the score lands under
    the session AND shows in the Scores view. Both server v2 and v3 accept
    ``score-create`` with a numeric value + traceId over this REST endpoint."""
    now = datetime.datetime.now(datetime.timezone.utc)
    trace_id = str(uuid.uuid4())
    meta = {**(metadata or {})}
    if session_id:
        meta["session_id"] = session_id
    trace_body = {"id": trace_id, "name": f"score:{name}",
                  "timestamp": now.isoformat(), "metadata": meta,
                  **({"sessionId": str(session_id)} if session_id else {})}
    score_body = {"id": str(uuid.uuid4()), "traceId": trace_id,
                  "name": name, "value": value, "dataType": data_type,
                  **({"comment": comment[:500]} if comment else {})}
    payload = {"batch": [
        {"id": str(uuid.uuid4()), "type": "trace-create",
         "timestamp": now.isoformat(), "body": trace_body},
        {"id": str(uuid.uuid4()), "type": "score-create",
         "timestamp": now.isoformat(), "body": score_body},
    ]}
    threading.Thread(target=_send, args=(payload,), daemon=True).start()


__all__ = ["available", "enabled", "record_generation", "record_score"]
