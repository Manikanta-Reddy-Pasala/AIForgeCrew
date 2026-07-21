"""Map-reduce summariser for large documents (hundreds of pages).

A chat/tool attachment stores only a short excerpt (``chat_media._DESC_CAP``)
as its description — fine for a 2-page memo, useless for a 400-page report
where the model would otherwise only ever see the first few pages. This module
turns the FULL extracted text into a real summary:

    MAP     split the text into windows, summarise each window on its own
    REDUCE  fold the window summaries into one; recurse while still too big

Every step is one bounded LLM call, run SEQUENTIALLY, so an arbitrarily long
document collapses to a fixed-size summary without ever exceeding the model's
context. Best-effort — any failure falls back to a plain truncated excerpt so
an upload never breaks.

Tunables (env):
    AIFORGE_SUMMARY_WINDOW_CHARS  chars per map window        (default 12000)
    AIFORGE_SUMMARY_MAX_WINDOWS   hard cap on map LLM calls   (default 60)
    AIFORGE_DOC_SUMMARY_ENABLED   "0" disables map-reduce     (default on)
"""
from __future__ import annotations

import os

_MAP_TOKENS = 320          # per-window summary length
_REDUCE_TOKENS = 700       # final / intermediate fold length
_EXCERPT_FALLBACK = 6000   # chars kept when summarisation is off / fails


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _window_chars() -> int:
    return _int_env("AIFORGE_SUMMARY_WINDOW_CHARS", 14000)


def _max_windows() -> int:
    # 100 windows × 14k chars ≈ 1.4M chars ≈ 500+ pages before any truncation,
    # so a 400-page document folds in full.
    return _int_env("AIFORGE_SUMMARY_MAX_WINDOWS", 100)


def enabled() -> bool:
    return (os.environ.get("AIFORGE_DOC_SUMMARY_ENABLED", "1") or "1") != "0"


def _split(text: str, size: int) -> list[str]:
    """Split text into ~``size``-char windows on line boundaries (never mid
    line), so a window stays semantically whole."""
    lines = text.splitlines()
    windows: list[str] = []
    buf: list[str] = []
    used = 0
    for ln in lines:
        if used + len(ln) > size and buf:
            windows.append("\n".join(buf))
            buf, used = [], 0
        buf.append(ln)
        used += len(ln) + 1
    if buf:
        windows.append("\n".join(buf))
    return windows or ([text] if text else [])


def _map_summarize(role: str, chunk: str, idx: int, total: int) -> str:
    from aiforge_core.llm.client import complete
    prompt = (
        f"You are summarising part {idx} of {total} of a long document. "
        "Write a tight, factual summary of THIS part only — key points, names, "
        "numbers, decisions. No preamble.\n\n" + chunk)
    try:
        return (complete(role, [{"role": "user", "content": prompt}],
                         max_tokens=_MAP_TOKENS) or "").strip()
    except Exception:  # noqa: BLE001 — best-effort; a dead window is skipped
        return ""


def _reduce(role: str, partials: list[str]) -> str:
    """Fold window summaries into one. If the joined partials are themselves
    too big for a single window, group and reduce recursively first."""
    from aiforge_core.llm.client import complete
    partials = [p for p in partials if p.strip()]
    if not partials:
        return ""
    if len(partials) == 1:
        return partials[0]
    joined = "\n\n".join(partials)
    win = _window_chars()
    if len(joined) > win:
        # Recurse: summarise groups of partials down to a manageable set.
        groups = _split(joined, win)
        partials = [_map_summarize(role, g, i + 1, len(groups))
                    for i, g in enumerate(groups)]
        return _reduce(role, partials)
    prompt = (
        "Below are section summaries of one long document, in order. Combine "
        "them into a single coherent summary: an overview, then the main "
        "points/findings. Do not repeat yourself.\n\n" + joined)
    try:
        out = (complete(role, [{"role": "user", "content": prompt}],
                        max_tokens=_REDUCE_TOKENS) or "").strip()
        return out or joined
    except Exception:  # noqa: BLE001
        return joined


def summarize_text(text: str, role: str = "chat") -> str:
    """Map-reduce ``text`` into one summary. Small text → single LLM call;
    large text → windowed map then reduce. Returns "" only when there is no
    text or every LLM call fails and there is nothing to fall back to."""
    text = (text or "").strip()
    if not text:
        return ""
    if not enabled():
        return text[:_EXCERPT_FALLBACK]
    win = _window_chars()
    windows = _split(text, win)
    cap = _max_windows()
    truncated_note = ""
    if len(windows) > cap:
        truncated_note = (f"\n\n… (document exceeded {cap} summary windows; "
                          f"summarised the first {cap} of {len(windows)})")
        windows = windows[:cap]
    if len(windows) == 1:
        # Single window still benefits from a summary pass for a big-ish memo,
        # but a tiny doc is cheaper/clearer returned as-is.
        if len(text) <= win // 3:
            return text
        partials = [_map_summarize(role, windows[0], 1, 1)]
    else:
        partials = [_map_summarize(role, w, i + 1, len(windows))
                    for i, w in enumerate(windows)]
    summary = _reduce(role, partials)
    if not summary:
        return text[:_EXCERPT_FALLBACK]
    return summary + truncated_note


def summarize_document(path: str, role: str = "chat", mime: str = "") -> str:
    """Extract a document's full text (bounded by ``chat_media`` doc budget)
    then map-reduce it into a summary. Best-effort — "" when nothing readable."""
    from aiforge_core.runtime import chat_media
    text = chat_media.extract_text(path, mime)
    return summarize_text(text, role)


__all__ = ["summarize_text", "summarize_document", "enabled"]
