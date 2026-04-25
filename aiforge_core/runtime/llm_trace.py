"""LLM request/response trace logger.

Wraps a ``smolagents.LiteLLMModel`` instance so every ``generate`` call
emits a structured ``llm.call`` NDJSON event on the role logger. The
event carries the full ``messages`` list sent to the model and the
``response`` content, plus token counts and wall time — enough to see
exactly what each agent tick asked the LLM and what came back.

Picked up by the ``/api/trace/{id}/stream`` SSE endpoint via
``graph-runner.err``, so the UI renders it inline with smolagents Step
headers from stdout.
"""
from __future__ import annotations

import time
from typing import Any


def _msg_to_plain(msg: Any) -> dict:
    """Coerce a smolagents/LiteLLM ChatMessage-ish object to a plain dict."""
    if isinstance(msg, dict):
        return {
            "role": msg.get("role"),
            "content": msg.get("content"),
            "tool_calls": msg.get("tool_calls"),
        }
    role = getattr(msg, "role", None)
    content = getattr(msg, "content", None)
    tool_calls = getattr(msg, "tool_calls", None)
    return {"role": role, "content": content, "tool_calls": tool_calls}


def _truncate(s: Any, limit: int = 40000) -> Any:
    """Keep the event small enough for a single NDJSON line."""
    if not isinstance(s, str):
        return s
    if len(s) <= limit:
        return s
    return s[:limit] + f"…[truncated {len(s) - limit} chars]"


def _emit_call_event(logger, *, role: str, ticket: str | None,
                     messages, response, usage, dur_ms: int,
                     stream: bool, error: str | None = None) -> None:
    payload = {
        "role": role,
        "ticket": ticket,
        "agent_role": role,
        "stream": stream,
        "messages": [_msg_to_plain(m) for m in messages],
        "dur_ms": dur_ms,
    }
    if response is not None:
        payload["response"] = response
    if usage is not None:
        payload["usage"] = usage
    if error is not None:
        payload["error"] = error
    logger.info("llm.call", extra={"aiforge": payload})


def attach_trace(model, logger, *, role: str, ticket: str | None) -> None:
    """Wrap ``model.generate`` AND ``model.generate_stream`` to emit
    ``llm.call`` events. smolagents `ToolCallingAgent.step` picks
    generate_stream when `stream_outputs=True` (default), so the older
    generate-only wrapper missed every call.

    Idempotent: repeat calls with the same model don't double-wrap.
    """
    if getattr(model, "_aiforge_traced", False):
        return
    orig_generate = model.generate

    def traced_generate(messages, *args, **kwargs):
        t0 = time.monotonic()
        try:
            out = orig_generate(messages, *args, **kwargs)
        except Exception as exc:
            dur_ms = int((time.monotonic() - t0) * 1000)
            _emit_call_event(logger, role=role, ticket=ticket,
                             messages=messages, response=None, usage=None,
                             dur_ms=dur_ms, stream=False, error=repr(exc))
            raise
        dur_ms = int((time.monotonic() - t0) * 1000)
        response = _msg_to_plain(out)
        usage = None
        raw = getattr(out, "raw", None) or {}
        if isinstance(raw, dict):
            usage = raw.get("usage") or (raw.get("_hidden_params") or {}).get("usage")
        _emit_call_event(logger, role=role, ticket=ticket,
                         messages=messages, response=response, usage=usage,
                         dur_ms=dur_ms, stream=False)
        return out

    model.generate = traced_generate

    # Also wrap generate_stream — smolagents prefers this when
    # stream_outputs=True. We let the stream pass through unchanged but
    # tally the deltas at end-of-stream so the trace reflects the full
    # message + duration.
    orig_stream = getattr(model, "generate_stream", None)
    if orig_stream is not None:
        def traced_generate_stream(messages, *args, **kwargs):
            t0 = time.monotonic()
            chunks: list = []
            error_repr: str | None = None
            try:
                for chunk in orig_stream(messages, *args, **kwargs):
                    chunks.append(chunk)
                    yield chunk
            except Exception as exc:
                error_repr = repr(exc)
                raise
            finally:
                dur_ms = int((time.monotonic() - t0) * 1000)
                # Build a synthetic response from chunks: concat any text
                # delta + collect any tool_calls. smolagents
                # ChatMessageStreamDelta has .content (text delta) and
                # .tool_calls (list).
                text_parts: list[str] = []
                tool_calls_acc: list = []
                for c in chunks:
                    txt = getattr(c, "content", None)
                    if isinstance(txt, str):
                        text_parts.append(txt)
                    tcs = getattr(c, "tool_calls", None)
                    if tcs:
                        for tc in tcs:
                            tool_calls_acc.append({
                                "function": {
                                    "name": getattr(getattr(tc, "function", None), "name", None),
                                    "arguments": getattr(getattr(tc, "function", None), "arguments", None),
                                }
                            })
                response = {
                    "role": "assistant",
                    "content": "".join(text_parts),
                    "tool_calls": tool_calls_acc or None,
                }
                _emit_call_event(logger, role=role, ticket=ticket,
                                 messages=messages, response=response,
                                 usage=None, dur_ms=dur_ms, stream=True,
                                 error=error_repr)
        model.generate_stream = traced_generate_stream
    model._aiforge_traced = True
