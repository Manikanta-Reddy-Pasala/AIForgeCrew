"""Provider-aware prompt-cache markers.

Each cloud LLM has a different cache-control protocol:
- **Anthropic**: ``cache_control: {"type": "ephemeral"}`` block on
  the last 2 user messages.
- **Gemini**: explicit cache via ``Cached Content API`` (token-cost
  + TTL); we set ``cache: True`` on system + first long user msg
  through litellm/openai-compat passthrough.
- **OpenAI**: ``prompt_cache_key`` parameter (responses API) or
  automatic prefix caching (chat-completions ≥ 1024-token prefix).
- **Ollama Cloud**: no explicit cache yet; pass through.

KISS: one ``stamp(messages, model, provider)`` entry. Caller wraps
its outgoing payload before POSTing. Returns *new* messages list
(does not mutate input).

Toggle off via ``AIFORGE_PROMPT_CACHE=0`` (default on).
"""
from __future__ import annotations

import os
from typing import Iterable


def is_enabled() -> bool:
    return os.environ.get("AIFORGE_PROMPT_CACHE", "1") == "1"


def stamp(
    messages: list[dict], *, model: str, provider: str,
) -> list[dict]:
    """Return a new messages list with provider-specific cache hints."""
    if not is_enabled() or not messages:
        return list(messages)

    prov = (provider or "").lower()
    if prov == "anthropic":
        return _stamp_anthropic(messages)
    if prov == "gemini":
        return _stamp_gemini(messages)
    if prov in ("openai", "ollama_cloud"):
        # Both serve OpenAI-compat /chat/completions; OpenAI's prefix
        # cache is automatic when the same system prompt repeats.
        # Nothing to inject — leave messages untouched.
        return list(messages)
    return list(messages)


_KNOWN_TOOL_TAGS = frozenset({
    "file_read", "file_write", "file_patch", "bulk_edit",
    "java_refactor", "code_run", "bash", "glob", "grep", "batch",
    "lint", "tests", "undo", "web_search", "ask_explorer",
    "ask_user", "search_memory", "unified_memory_query",
    "todo_write", "todo_check", "enter_plan_mode", "exit_plan_mode",
    "dispatch_subagent",
})


def _extract_text_tool_use(content_blocks: list) -> list[dict]:
    """Scan text content for tool-call XML-ish tags and synthesise
    proper ``tool_use`` blocks. Three patterns supported:

    1. ``<tool_use>{"name":..,"arguments":..}</tool_use>``
       Generic GA text-protocol envelope.
    2. ``<file_read>{"path":..}</file_read>``
       Bare tag where the element name IS the tool name (Qwen3-
       Coder-Next on mlx-lm emits this when fed our 25-tool schema).
    3. ``<file_read><path>X</path><show_linenos>true</show_linenos></file_read>``
       Nested XML where each child element is one tool argument
       (Qwen3-Coder-Next variant when no JSON example seeded the
       prompt — model defaults to nested XML, common in Anthropic
       tool-use docs).

    Patterns 2/3 only fire for tag names in :data:`_KNOWN_TOOL_TAGS`
    so random `<thinking>` / `<summary>` blocks aren't mis-parsed.
    """
    import re as _re, json as _json
    pat_envelope = _re.compile(
        r"<tool_use>\s*(\{.*?\})\s*</tool_use>", _re.DOTALL,
    )
    pat_bare = _re.compile(
        r"<([a-z_][a-z0-9_]*)>\s*(\{.*?\})\s*</\1>", _re.DOTALL,
    )
    # Pattern 3 — nested XML. Outer tag = tool name, inner tags = args.
    # Greedy DOTALL match; ``[^<]*`` arg value (no nested elements
    # supported — KISS, model rarely emits multi-line arg bodies).
    pat_nested = _re.compile(
        r"<([a-z_][a-z0-9_]*)>"
        r"((?:\s*<[a-z_][a-z0-9_]*>[^<]*</[a-z_][a-z0-9_]*>\s*)+)"
        r"</\1>",
        _re.DOTALL,
    )
    pat_inner = _re.compile(
        r"<([a-z_][a-z0-9_]*)>([^<]*)</\1>", _re.DOTALL,
    )

    out: list[dict] = []
    for blk in content_blocks or []:
        if not isinstance(blk, dict) or blk.get("type") != "text":
            continue
        text = blk.get("text") or ""

        # Pattern 1 — envelope
        if "<tool_use>" in text:
            for m in pat_envelope.finditer(text):
                try:
                    payload = _json.loads(m.group(1))
                except Exception:
                    continue
                name = payload.get("name") or payload.get("tool")
                args = (payload.get("arguments")
                        or payload.get("args")
                        or payload.get("input")
                        or {})
                if not name:
                    continue
                out.append({
                    "type": "tool_use",
                    "id": f"text_{abs(hash(m.group(0))) & 0xfffff:x}",
                    "name": name,
                    "input": args if isinstance(args, dict) else {},
                })

        # Pattern 2 — bare tag with known tool name + JSON args.
        for m in pat_bare.finditer(text):
            tag = m.group(1)
            if tag not in _KNOWN_TOOL_TAGS:
                continue
            try:
                args = _json.loads(m.group(2))
            except Exception:
                continue
            out.append({
                "type": "tool_use",
                "id": f"bare_{abs(hash(m.group(0))) & 0xfffff:x}",
                "name": tag,
                "input": args if isinstance(args, dict) else {},
            })

        # Pattern 3 — nested XML <tool><arg>val</arg></tool>.
        # Skip ranges already matched by pattern 2 (bare-with-JSON)
        # to avoid double emission. Track consumed ranges via simple
        # set of start offsets.
        consumed_starts = {m.start() for m in pat_bare.finditer(text)}
        for m in pat_nested.finditer(text):
            if m.start() in consumed_starts:
                continue
            tag = m.group(1)
            if tag not in _KNOWN_TOOL_TAGS:
                continue
            inner = m.group(2)
            args: dict = {}
            for im in pat_inner.finditer(inner):
                k = im.group(1)
                v = im.group(2).strip()
                # Coerce types so the downstream tool sees correct
                # Python objects, not strings:
                #   "true"/"false" → bool
                #   integer / float → numeric
                #   JSON array / object → parsed list/dict
                #   anything else → string
                vl = v.lower()
                if vl in ("true", "false"):
                    args[k] = (vl == "true")
                    continue
                # JSON literal? (do_batch crashes with 'str has no
                # attribute get' when calls=[{...}] arrives as text.)
                if v and v[0] in "[{":
                    try:
                        args[k] = _json.loads(v)
                        continue
                    except Exception:
                        pass
                try:
                    args[k] = int(v)
                    continue
                except ValueError:
                    pass
                try:
                    args[k] = float(v)
                    continue
                except ValueError:
                    pass
                args[k] = v
            if args:
                out.append({
                    "type": "tool_use",
                    "id": f"xml_{abs(hash(m.group(0))) & 0xfffff:x}",
                    "name": tag,
                    "input": args,
                })
    return out


def apply_to_session(session: object, *, provider: str,
                     role: str = "?") -> None:
    """Monkey-patch ``session.raw_ask`` to:
      1. Stamp provider-aware cache markers on outgoing messages.
      2. Emit pre_llm + post_llm hook events with wall_ms + token
         delta so /api/runtime/perf shows LLM round-trip latency.

    KISS: wraps the existing generator. Idempotent (skips re-wrap).
    """
    if not is_enabled():
        return
    if getattr(session, "_aiforge_cache_wrapped", False):
        return
    orig = session.raw_ask  # type: ignore[attr-defined]
    model = getattr(session, "model", "")

    def _wrapped(messages, *a, **kw):
        import time as _t
        try:
            messages = stamp(list(messages), model=model, provider=provider)
        except Exception:
            pass
        # pre_llm event — record_step only (no wall_ms yet).
        try:
            from aiforge_core.doer.ga_tools import hooks as _hk
            _hk.emit_step(event="pre_llm", name=f"{provider}:{model}",
                          wall_ms=0,
                          extra={"role": role,
                                 "msg_count": len(messages or [])})
        except Exception:
            pass
        # Snapshot the outgoing messages so the post-call ``llm.call``
        # event can include them (full chat history) for the UI's
        # /api/llm-trace endpoint.
        _msgs_snapshot = list(messages or [])
        t0 = _t.time()
        # raw_ask returns a generator — wrap so the post_llm event
        # fires after the model finishes streaming, capturing wall_ms.
        try:
            gen = orig(messages, *a, **kw)
        except Exception as exc:
            _post_llm(role, provider, model, t0, exc=str(exc)[:200],
                      session=session)
            raise

        def _drain():
            try:
                value = yield from gen
            except Exception as exc:
                _post_llm(role, provider, model, t0,
                          exc=str(exc)[:200], session=session)
                _emit_llm_call(role, provider, model, t0,
                               messages=_msgs_snapshot, response=None,
                               error=str(exc)[:300])
                raise
            # Synthesise tool_use blocks from text-protocol tags
            # (`<tool_use>{...}</tool_use>`) when the model emits
            # them instead of native OpenAI tool_calls. Idempotent:
            # only fires when no native tool_use blocks present.
            try:
                if value and not any(
                    isinstance(b, dict) and b.get("type") == "tool_use"
                    for b in value
                ):
                    synth = _extract_text_tool_use(value)
                    if synth:
                        value = list(value) + synth
            except Exception:
                pass
            _post_llm(role, provider, model, t0, session=session,
                      content_blocks=value)
            _emit_llm_call(role, provider, model, t0,
                           messages=_msgs_snapshot, response=value)
            return value
        return _drain()

    session.raw_ask = _wrapped  # type: ignore[assignment]
    session._aiforge_cache_wrapped = True  # type: ignore[attr-defined]


def _post_llm(role: str, provider: str, model: str, t0: float,
              *, exc: str | None = None,
              session: object | None = None,
              content_blocks=None) -> None:
    import time as _t
    try:
        from aiforge_core.doer.ga_tools import hooks as _hk
        wall_ms = int((_t.time() - t0) * 1000)
        extra = {"role": role}
        if exc:
            extra["err"] = exc
        # Diagnostic: capture tool_use count + first text head from
        # content_blocks returned by raw_ask. Always captured when
        # AIFORGE_DEBUG_LLM=1; cheap (one list scan).
        if (content_blocks is not None
                and os.environ.get("AIFORGE_DEBUG_LLM", "0") == "1"):
            try:
                tool_use = 0
                text_head = ""
                for blk in content_blocks or []:
                    if not isinstance(blk, dict):
                        continue
                    bt = blk.get("type")
                    if bt == "tool_use":
                        tool_use += 1
                    elif bt == "text" and not text_head:
                        text_head = (blk.get("text") or "")[:300]
                extra["tool_use_count"] = tool_use
                extra["resp_head"] = text_head
                extra["block_count"] = len(content_blocks or [])
            except Exception:
                pass
        _hk.emit_step(
            event="post_llm",
            name=f"{provider}:{model}",
            wall_ms=wall_ms,
            extra=extra,
        )
    except Exception:
        pass


def _emit_llm_call(role: str, provider: str, model: str, t0: float,
                   *, messages, response, error: str | None = None) -> None:
    """Emit a structured ``llm.call`` log line. Picked up by the
    ``/api/llm-trace/{id}`` endpoint via graph-runner.err so the UI
    can replay full chat history per ticket. KISS: bound message body
    to keep one NDJSON line manageable."""
    if os.environ.get("AIFORGE_LLM_TRACE", "1") != "1":
        return
    import time as _t
    try:
        from aiforge_core.runtime.logging_setup import get_logger
        logger = get_logger(f"llm.{role}")
        # Best-effort ticket id resolution from current task context.
        ticket = os.environ.get("AIFORGE_CURRENT_TICKET") or None
        plain_msgs = []
        for m in (messages or []):
            if isinstance(m, dict):
                content = m.get("content")
                if isinstance(content, list):
                    parts = []
                    for c in content:
                        if isinstance(c, dict):
                            parts.append(c.get("text") or c.get("content") or "")
                    content = "\n".join(p for p in parts if p)[:8000]
                elif isinstance(content, str):
                    content = content[:8000]
                plain_msgs.append({
                    "role": m.get("role"),
                    "content": content,
                })
        # Compact response — capture text content blocks only.
        resp_text = ""
        if isinstance(response, list):
            for blk in response:
                if isinstance(blk, dict) and blk.get("type") == "text":
                    resp_text += blk.get("text") or ""
        elif isinstance(response, str):
            resp_text = response
        resp_text = resp_text[:8000]
        payload = {
            "agent_role": role,
            "ticket": ticket,
            "model": model,
            "provider": provider,
            "dur_ms": int((_t.time() - t0) * 1000),
            "messages": plain_msgs,
            "response": resp_text,
        }
        if error:
            payload["error"] = error
        logger.info("llm.call", extra={"aiforge": payload})
    except Exception:
        pass


def cache_key_for(model: str, role: str) -> str:
    """Stable key per (role, model) — used by OpenAI Responses API
    `prompt_cache_key` param. Two distinct roles never share cache."""
    return f"aiforge:{role}:{_short_model(model)}"


# ───────── helpers ─────────────────────────────────────────────────


def _stamp_anthropic(messages: list[dict]) -> list[dict]:
    """Anthropic ephemeral cache on last 2 user messages."""
    out = [dict(m) for m in messages]
    user_idxs = [i for i, m in enumerate(out) if m.get("role") == "user"]
    for idx in user_idxs[-2:]:
        msg = out[idx]
        c = msg.get("content")
        if isinstance(c, str):
            msg["content"] = [{
                "type": "text", "text": c,
                "cache_control": {"type": "ephemeral"},
            }]
        elif isinstance(c, list) and c:
            new_c = list(c)
            last = dict(new_c[-1])
            last["cache_control"] = {"type": "ephemeral"}
            new_c[-1] = last
            msg["content"] = new_c
        out[idx] = msg
    # System block too (most ROI for system prompt cache).
    if out and out[0].get("role") == "system":
        sys = dict(out[0])
        sc = sys.get("content")
        if isinstance(sc, str):
            sys["content"] = [{
                "type": "text", "text": sc,
                "cache_control": {"type": "persistent"},
            }]
        out[0] = sys
    return out


def _stamp_gemini(messages: list[dict]) -> list[dict]:
    """Gemini doesn't expose cache via the OpenAI-compat shim that
    AI Studio fronts. Best we can do via that path is a no-op; full
    cache would need switching to the native ``cachedContents`` API.
    Keep as stub so the surface stays uniform with anthropic.
    """
    return list(messages)


def _short_model(model: str) -> str:
    if "/" in model:
        return model.rsplit("/", 1)[-1]
    return model
