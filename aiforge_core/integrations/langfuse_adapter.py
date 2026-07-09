"""langfuse adapter — LLM observability traces (``pip install
aiforgecrew[tracing]``).

Mirrors every LLM completion to a (self-hosted) Langfuse server so runs are
browsable per role/model with latency + input/output — ALONGSIDE the
existing file-based tracing (observability/chat_trace/perf), which stays the
source of truth. Adapter contract: ``available()``/``enabled()`` probes, one
narrow capability, raises nothing into the hot path (the caller wraps).

Enable purely by env (config-driven, no code flag):
    LANGFUSE_HOST=http://127.0.0.1:3000     # your self-hosted server
    LANGFUSE_PUBLIC_KEY=pk-lf-…
    LANGFUSE_SECRET_KEY=sk-lf-…
Optional: AIFORGE_LANGFUSE_DISABLE=1 kills it even with keys set;
AIFORGE_LANGFUSE_MAX_CHARS caps per-field payload size (default 8000).
"""
from __future__ import annotations

import atexit
import os

_client = None
_client_failed = False


def available() -> bool:
    try:
        import langfuse  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def enabled() -> bool:
    if str(os.environ.get("AIFORGE_LANGFUSE_DISABLE", "")).strip().lower() \
            in ("1", "true", "yes", "on"):
        return False
    return bool(os.environ.get("LANGFUSE_PUBLIC_KEY")
                and os.environ.get("LANGFUSE_SECRET_KEY")) and available()


def _get():
    """Singleton client (the SDK batches + flushes on its own thread)."""
    global _client, _client_failed
    if _client is None and not _client_failed:
        try:
            from langfuse import Langfuse
            _client = Langfuse()   # reads LANGFUSE_* env itself
            atexit.register(lambda: _client and _client.flush())
        except Exception:  # noqa: BLE001 — a broken client must not retry per call
            _client_failed = True
    return _client


def _cap() -> int:
    try:
        return max(500, int(os.environ.get("AIFORGE_LANGFUSE_MAX_CHARS", "8000")))
    except ValueError:
        return 8000


def record_generation(*, role: str, model: str = "", messages=None,
                      output: str = "", latency_ms: int = 0,
                      error: str = "", session_id=None,
                      metadata: dict | None = None) -> None:
    """One LLM completion → one Langfuse generation. Raises on failure —
    the caller (llm.client) wraps in try/except so tracing can never break
    a turn."""
    lf = _get()
    if lf is None:
        return
    cap = _cap()
    msgs = [{"role": m.get("role"), "content": str(m.get("content"))[:cap]}
            for m in (messages or []) if isinstance(m, dict)]
    kwargs = dict(
        name=f"llm:{role}",
        model=model or None,
        input=msgs,
        output=(output or "")[:cap],
        metadata={**(metadata or {}), "role": role,
                  **({"error": error[:500]} if error else {}),
                  **({"session_id": session_id} if session_id else {})},
    )
    if latency_ms:
        kwargs["metadata"]["latency_ms"] = int(latency_ms)
    if hasattr(lf, "generation"):                 # SDK v2
        lf.generation(**kwargs)
    elif hasattr(lf, "start_generation"):         # SDK v3 (OTEL)
        g = lf.start_generation(name=kwargs["name"], model=kwargs["model"],
                                input=kwargs["input"],
                                metadata=kwargs["metadata"])
        g.update(output=kwargs["output"])
        g.end()
    # else: unknown SDK surface — silently skip (file tracing still has it)


__all__ = ["available", "enabled", "record_generation"]
