from __future__ import annotations


def _t_web_search(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import web_search
    return web_search.web_search(args, cwd)


def _t_web_fetch(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import web_search
    return web_search.web_fetch(args, cwd)


def _t_web_crawl(args: dict, cwd: str) -> dict:
    """Fetch a URL as clean markdown and file it as a work/web/<slug> dossier
    (crawl4ai when installed, tag-strip fetch fallback)."""
    from aiforge_core.runtime.tools import web_ingest
    return web_ingest.web_crawl(args, cwd)
