"""Keeping the model context in budget: size limits, trimming and condensing
history, and guarding against phantom tool calls."""
from __future__ import annotations

import contextlib
import os


class _CtxLimits:
    """The trimming budget, read from env once instead of by each closure."""

    __slots__ = ("keep_invocations", "max_contents", "strategy",
                 "max_chars", "max_part_chars", "min_keep")

    def __init__(self) -> None:
        self.keep_invocations = _int_env("AIFORGE_CONTEXT_KEEP_INVOCATIONS", 12)
        self.max_contents = _int_env("AIFORGE_CONTEXT_MAX_CONTENTS", 60)
        self.strategy = os.environ.get("AIFORGE_CONDENSER_STRATEGY", "").strip()
        # ~4 chars/token; budget in tokens then converted to a char ceiling.
        max_tokens = _int_env("AIFORGE_CONTEXT_MAX_TOKENS",
                              int(_context_window() * _history_frac()))
        self.max_chars = max(4000, max_tokens * 4)
        self.max_part_chars = _int_env("AIFORGE_CONTEXT_MAX_PART_CHARS", 24000)
        self.min_keep = max(4, _int_env("AIFORGE_CONTEXT_MIN_KEEP", 8))


def _int_env(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except (TypeError, ValueError):
        return default


def _context_window() -> int:
    """The Doer model's EFFECTIVE window (per-model value → operator setting →
    auto-detected → default) — the same source chat uses. It read only the
    global setting (128K default), so a 256K model was trimmed as if 128K."""
    try:
        from aiforge_core.config import model_registry
        win = int(model_registry.effective_context_window("doer"))
        if win > 0:
            return win
    except Exception:  # noqa: BLE001
        pass
    try:
        from aiforge_core.config import runtime_settings as _rs
        return int(_rs.get("context_window") or 131072)
    except Exception:  # noqa: BLE001
        return 131072


def _history_frac() -> float:
    """The same compaction trigger the simple ReAct loop uses (80% of the
    window by default), so team mode and tickets trim at the same point.
    Soft-fails to 0.8 when the import is unavailable.
    """
    try:
        from aiforge_core.runtime.chat_agent._context._window import _history_fraction
        return _history_fraction()
    except Exception:  # noqa: BLE001
        return 0.8


def _text_of(c) -> str:
    try:
        return " ".join(p.text for p in (c.parts or [])
                        if getattr(p, "text", None))
    except Exception:  # noqa: BLE001
        return ""


def _dedupe_adjacent_user(contents: list) -> list:
    """Drop adjacent duplicate user-text contents. single_turn nodes
    append their seed input to the SHARED session events (shallow
    session copy in ADK's wrapper), so every chat agent replays the
    ticket+memory seed twice back-to-back — pure token waste."""
    out: list = []
    for c in contents:
        dup = (out and getattr(c, "role", "") == "user"
               and getattr(out[-1], "role", "") == "user"
               and _text_of(c) and _text_of(c) == _text_of(out[-1]))
        if not dup:
            out.append(c)
    return out


def _content_chars(c) -> int:
    """Estimate a content's character weight — text parts plus a rough
    size for function_response payloads (the big tool results)."""
    total = 0
    for p in (getattr(c, "parts", None) or []):
        t = getattr(p, "text", None)
        if t:
            total += len(t)
            continue
        fr = getattr(p, "function_response", None)
        if fr is not None:
            with contextlib.suppress(Exception):
                total += len(str(getattr(fr, "response", "") or ""))
    return total


def _shorten(s: str, cap: int) -> str:
    """Head + tail of ``s``, middle elided, when it exceeds ``cap``."""
    if len(s) <= cap:
        return s
    half = max(1000, cap // 2)
    return (s[:half] + f"\n…[truncated {len(s) - cap} "
            f"chars to fit context]…\n" + s[-half:])


def _capped_part(p, cap: int, gtypes):
    """``p`` truncated to ``cap`` if it is oversized text or a fat function
    response; the part itself otherwise."""
    t = getattr(p, "text", None)
    if t and len(t) > cap:
        return gtypes.Part.from_text(text=_shorten(t, cap))
    fr = getattr(p, "function_response", None)
    if fr is None:
        return p
    resp = getattr(fr, "response", None)
    if len(str(resp or "")) <= cap or not isinstance(resp, dict):
        return p
    half = max(1000, cap // 2)
    trimmed = {k: (_shorten(v, cap) if isinstance(v, str) and len(v) > half else v)
               for k, v in resp.items()}
    return gtypes.Part.from_function_response(
        name=getattr(fr, "name", "") or "", response=trimmed)


def _cap_content(c, cap: int):
    """Return ``c`` if within the per-content cap, else a rebuilt copy
    with oversized text / function_response payloads truncated (head +
    tail kept, middle elided). Falls back to the original on any error
    so a structure we don't understand is never dropped."""
    if cap <= 0 or _content_chars(c) <= cap:
        return c
    try:
        from google.genai import types as gtypes
        parts = [_capped_part(p, cap, gtypes)
                 for p in (getattr(c, "parts", None) or [])]
        return gtypes.Content(role=getattr(c, "role", "user"), parts=parts)
    except Exception:  # noqa: BLE001
        return c


def _window(contents: list, n: int, adjust, is_human) -> list:
    """The seed user message plus the last ``n`` contents, split adjusted so a
    function response is never orphaned from its call."""
    if n <= 0 or len(contents) <= n:
        return list(contents)
    split = len(contents) - n
    with contextlib.suppress(Exception):
        split = adjust(contents, split)
    head_seed = [c for c in contents[:split] if is_human(c)][:1]
    return head_seed + list(contents[split:])


def _tail_trimmer(lim: "_CtxLimits", adjust, is_human):
    """ADK ``custom_filter``: dedupe seed echoes, cap oversized contents, then
    keep the seed user message + the most recent contents under BOTH the count
    cap and the token budget (item-3: protect slow 120B models)."""
    def _tail_trim(contents):
        contents = [_cap_content(c, lim.max_part_chars)
                    for c in _dedupe_adjacent_user(contents)]
        keep_n = lim.max_contents if lim.max_contents > 0 else len(contents)
        out = _window(contents, keep_n, adjust, is_human)
        # Token-budget pass: if the kept window is still too heavy, shrink
        # the tail window until under the char ceiling (or we hit min_keep).
        while (lim.max_chars > 0 and keep_n > lim.min_keep
               and sum(_content_chars(c) for c in out) > lim.max_chars):
            keep_n -= 4
            out = _window(contents, keep_n, adjust, is_human)
        return out
    return _tail_trim


def _as_events(contents: list) -> list[dict]:
    return [{"type": "content", "role": getattr(c, "role", ""),
             "text": " ".join(getattr(p, "text", "") or ""
                              for p in (getattr(c, "parts", None) or [])
                              if getattr(p, "text", None))}
            for c in contents]


def _condensing_filter(tail_trim, strategy: str):
    """Sub #4: optional aggressive condenser layered over the content-tail
    trim. ``amortized`` compresses the oldest half into one synthetic block;
    ``recent`` is keep-tail only."""
    from aiforge_core.runtime.condensers import condense

    def _filter(contents):
        contents = tail_trim(contents)
        condensed = condense(_as_events(contents), strategy)
        # ADK custom_filter must return list[Content]; align tail-N of the
        # condensed events to tail-N of the real contents and keep those
        # objects. ``amortized`` prepends one synthetic block, which we pass
        # through as a fresh Content.
        summarised = bool(condensed) and condensed[0].get("role") == "condenser"
        keep_n = len(condensed) - (1 if summarised else 0)
        tail = list(contents[-keep_n:]) if keep_n > 0 else []
        if not summarised:
            return tail
        from google.genai import types as gtypes
        summary = gtypes.Content(
            role="user",
            parts=[gtypes.Part.from_text(text=condensed[0]["text"])])
        return [summary] + tail
    return _filter


def _run_repo_root() -> str:
    """This run's repo: the request context (team chat sets it per run), then
    AIFORGE_REPO_ROOT (the ticket runner's worktree)."""
    from aiforge_core.runtime import request_context
    return request_context.get_repo_root() or ""


def _phantom_tool_guard() -> list:
    """Keep the pipeline alive when a text agent emits a hallucinated
    function_call — ADK would otherwise raise "Tool X not found" and abort the
    whole run. See tool_error_plugin. The perf observer goes FIRST: it returns
    None from every callback, so it sees every call and changes none."""
    plugins: list = []
    try:
        from ..perf_plugin import PerfPlugin
        plugins.append(PerfPlugin())
    except Exception:  # noqa: BLE001 — perf is optional
        pass
    try:
        from ..tool_error_plugin import PhantomToolGuardPlugin
        plugins.append(PhantomToolGuardPlugin())
    except Exception:  # noqa: BLE001 — resilience is best-effort
        pass
    return plugins
