"""Reasoning-trace stripping, text extraction, and garbage/empty-answer
heuristics. Leaf module — depends only on the stdlib ``re``."""
from __future__ import annotations

import re

# Reasoning models (qwen3-coder, deepseek-r1, …) sometimes emit their chain of
# thought inside the *content* field wrapped in <think>…</think> (or the
# lookalikes below) instead of the separate reasoning_content channel. When
# they do, the real answer is whatever sits AFTER the closing tag — often
# nothing, because the model spent its whole budget thinking. Strip the block
# so the caller never sees raw reasoning as the answer, and so a think-only
# reply collapses to "" and trips the garbage/retry path.
# Reasoning models put their chain of thought at the START, then the answer.
# We only strip a LEADING think block (after optional whitespace) — NOT blocks
# mid-content, so a legitimate answer that CONTAINS a <think>/<reasoning> literal
# (code emitting this codebase's own tag regex, an XML/prompt template) is left
# intact instead of being silently corrupted.
_THINK_LEAD_RE = re.compile(
    r"^\s*<(think|thought|reasoning|thinking)\b[^>]*>.*?</\1>\s*",
    re.IGNORECASE | re.DOTALL,
)
# Closing-tag-ONLY leading reasoning: many chat templates (qwen3, deepseek-r1)
# inject the OPENING <think> into the prompt, so the model's content starts with
# raw reasoning and the FIRST tag it emits is the closer — "reasoning…</think>the
# answer". Strip from the start up to that first closer, but ONLY when NO opener
# appears before it (else _THINK_LEAD_RE already handled the paired block, and a
# code/XML literal like re.compile(r"<think>.*?</think>") keeps its opener so it
# is preserved) and NOT when the answer opens with a code fence.
_THINK_CLOSE_ONLY_RE = re.compile(
    r"^\s*(?!```)"
    r"(?:(?!<(?:think|thought|reasoning|thinking)\b).)*?"
    r"</(?:think|thought|reasoning|thinking)>\s*",
    re.IGNORECASE | re.DOTALL,
)
# A LEADING unclosed opener: the stream ran out mid-thought — everything from the
# opener to end-of-string is reasoning with no answer following → drop it.
_THINK_OPEN_RE = re.compile(
    r"^\s*<(think|thought|reasoning|thinking)\b[^>]*>.*\Z",
    re.IGNORECASE | re.DOTALL,
)


def _strip_think(text: str) -> str:
    if "<" not in text:
        return text
    # Strip one-or-more leading closed think blocks (reasoning-then-answer).
    prev = None
    while prev != text:
        prev = text
        text = _THINK_LEAD_RE.sub("", text, count=1)
    # Then a leading closing-tag-only reasoning block (opener consumed by the
    # chat template).
    text = _THINK_CLOSE_ONLY_RE.sub("", text, count=1)
    # …then a leading unclosed opener (pure reasoning, no answer).
    text = _THINK_OPEN_RE.sub("", text)
    return text.strip()


def _msg_text(msg: dict) -> str:
    """Answer text from an assistant MESSAGE dict: think-stripped ``content``,
    else the ``reasoning_content`` channel (also think-stripped). Shared by the
    text path and the native path's plain-content branch so both recover a
    reasoning-only reply identically."""
    content = _strip_think((msg.get("content") or "").strip())
    if content:
        return content
    # content was empty or pure <think> — fall back to the reasoning channel,
    # but strip any nested think markers there too (some proxies double-wrap).
    return _strip_think((msg.get("reasoning_content") or "").strip())


def _extract_text(resp_body: dict) -> str:
    msg = (resp_body.get("choices") or [{}])[0].get("message", {}) or {}
    return _msg_text(msg)


def _is_garbage(text: str, *, allow_empty_json: bool = False) -> bool:
    """Heuristic for a 200-OK but useless response.

    Triggers fallback when:
      - empty after trim
      - just an mlx-lm tool-call dump fragment ("<tool_call>" with no body)
      - well-known stop-token leak ("<|im_end|>" alone)
    """
    if not text or not text.strip():
        return True
    t = text.strip()
    # Valid EMPTY JSON containers are a legitimate answer ONLY for the roles that
    # PRODUCE JSON lists/objects (the learner/fact-distiller reply "[]" = nothing
    # to record). For a chat/doer answer a bare "[]" is nonsense → still garbage
    # (retry). Gated by ``allow_empty_json`` (caller passes it for fast/
    # structured roles) so the learner fix doesn't leak into conversational output.
    if allow_empty_json and t in ("[]", "{}"):
        return False
    # A very short reply is garbage ONLY when it carries no actual content — a
    # stray punctuation/markup fragment (".", "…", "<>", "``"). A short but real
    # answer ("4", "no", "ok", "42", a yes/no, a single-digit result) is a
    # LEGITIMATE response and must not be flagged empty: the old bare `len < 3`
    # rejected "4" (the correct answer to "2+2"), which then retried and failed
    # as "model didn't respond" on a model that had answered correctly.
    if len(t) < 3 and not any(c.isalnum() for c in t):
        return True
    if t in ("<tool_call>", "</tool_call>", "<|im_end|>", "<|endoftext|>"):
        return True
    return False


def _append_no_think(messages: list[dict]) -> list[dict]:
    """Return a copy of ``messages`` nudging the model to answer WITHOUT its
    reasoning phase — used only on an empty-response retry. Appends ' /no_think'
    to the last user turn (the Qwen3 / DeepSeek-R1 convention); harmless text for
    models that ignore it."""
    out = [dict(m) for m in (messages or [])]
    for m in reversed(out):
        if m.get("role") == "user":
            cur = str(m.get("content") or "").rstrip()
            # idempotent — never append twice (a fast role already coaxed on
            # attempt 0 must not become "… /no_think /no_think" on a retry).
            if not cur.endswith("/no_think"):
                cur = cur + " /no_think"
            m["content"] = cur
            return out
    out.append({"role": "user", "content": "/no_think"})
    return out
