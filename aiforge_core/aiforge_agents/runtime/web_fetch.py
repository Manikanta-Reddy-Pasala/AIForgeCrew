"""URL auto-learn — fetch + plain-text + summarise.

Used by Understander when the ticket body contains URLs. We pull each
URL, strip HTML to text, and let the LLM produce a tight summary that
gets embedded into context_md. That way agents can ground decisions on
external docs / GitHub READMEs / vendor specs without anyone manually
copy-pasting them in.

Failure-soft: any fetch / parse / LLM error returns empty string for
that URL — no exception escapes the helper.
"""
from __future__ import annotations

import re
from typing import Iterable

import httpx


_URL_RE = re.compile(
    r"https?://[^\s<>\"\'\)\]\}]+",
    re.IGNORECASE,
)

_TAG_RE = re.compile(r"<[^>]+>")
_MULTISPACE_RE = re.compile(r"\s+")


def extract_urls(text: str) -> list[str]:
    """All distinct http/https URLs in the input. Order-preserving."""
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in _URL_RE.finditer(text):
        u = m.group(0).rstrip(".,;:!?")
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def fetch_text(url: str, *, max_bytes: int = 200_000,
               timeout: float = 12.0) -> str:
    """GET + strip HTML + collapse whitespace. Best-effort."""
    try:
        r = httpx.get(
            url, timeout=timeout, follow_redirects=True,
            headers={"User-Agent": "aiforge-agents/0.1 (web-fetch)"},
        )
        r.raise_for_status()
    except Exception:
        return ""
    body = r.text or ""
    if len(body) > max_bytes:
        body = body[:max_bytes]
    # Heuristic strip: drop scripts/styles, then tags, then collapse.
    body = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ",
                  body, flags=re.IGNORECASE | re.DOTALL)
    body = _TAG_RE.sub(" ", body)
    body = _MULTISPACE_RE.sub(" ", body).strip()
    return body


def summarise(url: str, text: str, *, model: str = "",
              max_chars: int = 1500) -> str:
    """LLM-summarise the fetched text. Returns short markdown.

    Uses the agent LLM client so config + base_url stay shared. On any
    failure returns a head-truncated raw snippet — better than nothing.
    """
    if not text.strip():
        return ""
    try:
        from aiforge_core.aiforge_agents.runtime import llm_client
        system = (
            "You summarise external pages for a code agent. "
            "Output 4–8 short bullets capturing facts the agent would "
            "need: APIs, conventions, gotchas, examples. No prose. "
            "Stay under 800 chars total."
        )
        user = f"# URL: {url}\n\n# Page content (truncated)\n{text[:6000]}"
        out = llm_client.call_text(
            model=model or "qwen3-coder-next",
            system=system, user=user,
            temperature=0.0, max_tokens=600,
        )
        return (out or "").strip()[:max_chars]
    except Exception:
        return text[:max_chars]


def fetch_and_summarise(urls: Iterable[str]) -> str:
    """For each URL, fetch + summarise + render as a markdown block.

    Empty input or all-failed fetches return ''. Caller can drop the
    block into Understander's prompt directly.
    """
    chunks: list[str] = []
    for u in list(urls)[:5]:  # cap to 5 URLs per ticket
        text = fetch_text(u)
        if not text:
            continue
        summary = summarise(u, text)
        if not summary:
            continue
        chunks.append(f"### {u}\n{summary}")
    if not chunks:
        return ""
    return "## External references (auto-fetched)\n\n" + "\n\n".join(chunks) + "\n"
