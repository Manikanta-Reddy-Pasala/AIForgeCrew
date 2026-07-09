"""crawl4ai adapter — URL → LLM-ready markdown (``pip install
aiforgecrew[crawl]``; needs a playwright browser: ``playwright install
chromium``).

One capability: :func:`crawl` returns ``{markdown, title, url}``. Raises on
any failure (lib missing, browser missing, fetch error) — the domain caller
(:mod:`aiforge_core.runtime.tools.web_ingest`) owns the plain-HTTP fallback.
"""
from __future__ import annotations


def available() -> bool:
    try:
        import crawl4ai  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def crawl(url: str, *, timeout_s: int = 60) -> dict:
    """Fetch ``url`` with a real (headless) browser and return clean markdown.
    Runs crawl4ai's async API to completion on a private event loop. Safe to
    call from BOTH sync code and code already inside a running event loop
    (ADK runs sync tools on the loop thread — a bare ``asyncio.run`` would
    raise ``RuntimeError`` there and silently kill the engine)."""
    import asyncio

    from crawl4ai import AsyncWebCrawler

    async def _inner() -> dict:
        async with AsyncWebCrawler() as crawler:
            res = await crawler.arun(url=url)
            md = getattr(res, "markdown", None)
            # crawl4ai >= 0.4 wraps markdown in an object; older returns str.
            text = getattr(md, "raw_markdown", None) or (
                md if isinstance(md, str) else "") or ""
            meta = getattr(res, "metadata", None) or {}
            return {"markdown": text,
                    "title": str(meta.get("title") or ""),
                    "url": url}

    async def _run() -> dict:
        # One deadline over the WHOLE crawl incl. browser launch — a hung
        # chromium spawn must not block a chat turn indefinitely.
        return await asyncio.wait_for(_inner(), timeout=timeout_s)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_run())        # plain sync caller
    # Already inside a loop → run on a dedicated thread with its own loop.
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(asyncio.run, _run()).result(timeout=timeout_s + 30)


__all__ = ["available", "crawl"]
