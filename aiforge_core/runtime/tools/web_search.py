"""Keyless web search + page fetch for the chat agent.

When the agent is stuck — an unfamiliar error, a library API it can't recall,
a config flag it doesn't know — it can search the open web and read a result
page. Backed by DuckDuckGo's HTML endpoint, so NO API key is required.

Tools:
  web_search(query, limit)  → ranked [{title, url, snippet}]
  web_fetch(url, max_chars) → readable text of one page (tags stripped)

Knobs (env, all optional):
  AIFORGE_WEB_SEARCH_DISABLE=1   hard-off (returns a clear error)
  AIFORGE_WEB_SEARCH_RESULTS     default result count (default 5)
  AIFORGE_WEB_TIMEOUT_S          per-request timeout (default 12)

Soft-error contract: every function returns ``{"ok": bool, ...}`` and never
raises into the agent loop.
"""
from __future__ import annotations

import html
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from aiforge_core.net.ssl import context_for as _ssl_context_for

_DDG_HTML = "https://html.duckduckgo.com/html/"
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_FETCH_CAP = 1_000_000          # raw bytes read ceiling
_TEXT_DEFAULT = 6000            # default chars returned by web_fetch


def _disabled() -> bool:
    return str(os.environ.get("AIFORGE_WEB_SEARCH_DISABLE", "")).strip().lower() \
        in ("1", "true", "yes", "on")


def _timeout() -> float:
    try:
        return float(os.environ.get("AIFORGE_WEB_TIMEOUT_S", "12"))
    except ValueError:
        return 12.0


def _default_limit() -> int:
    try:
        return max(1, int(os.environ.get("AIFORGE_WEB_SEARCH_RESULTS", "5")))
    except ValueError:
        return 5


def _get(url: str, *, data: bytes | None = None) -> str:
    req = urllib.request.Request(
        url, data=data,
        headers={"User-Agent": _UA, "Accept": "text/html,application/xhtml+xml",
                 "Accept-Language": "en-US,en;q=0.9"},
        method="POST" if data is not None else "GET")
    with urllib.request.urlopen(
            req, timeout=_timeout(), context=_ssl_context_for(url)) as r:
        raw = r.read(_FETCH_CAP + 1)
    # honour the response charset loosely; default utf-8
    return raw[:_FETCH_CAP].decode("utf-8", "replace")


def _strip_tags(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s).strip()


def _ddg_real_url(href: str) -> str:
    """DDG wraps results in a redirector (//duckduckgo.com/l/?uddg=…). Pull the
    real target out so the agent gets a clean, directly-fetchable URL."""
    if "uddg=" in href:
        try:
            q = urllib.parse.urlparse(href).query
            uddg = urllib.parse.parse_qs(q).get("uddg")
            if uddg:
                return urllib.parse.unquote(uddg[0])
        except Exception:  # noqa: BLE001
            pass
    if href.startswith("//"):
        return "https:" + href
    return href


_RESULT_A = re.compile(
    r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.DOTALL)
_SNIPPET = re.compile(
    r'class="result__snippet"[^>]*>(.*?)</a>', re.DOTALL)


def _api_search(query: str, limit: int) -> "list[dict] | None":
    """Reliable keyed providers, tried before the DDG HTML scrape when a key is
    configured. Returns a results list, or None when no key / the call failed."""
    import json as _json
    tav = os.environ.get("AIFORGE_TAVILY_API_KEY", "").strip()
    if tav:
        try:
            payload = _json.dumps({"api_key": tav, "query": query,
                                   "max_results": limit}).encode()
            req = urllib.request.Request(
                "https://api.tavily.com/search", data=payload,
                headers={"Content-Type": "application/json", "User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=_timeout(),
                                        context=_ssl_context_for("https://api.tavily.com")) as r:
                d = _json.loads(r.read().decode("utf-8", "replace"))
            return [{"title": x.get("title", ""), "url": x.get("url", ""),
                     "snippet": x.get("content", "")} for x in d.get("results", [])][:limit]
        except Exception:  # noqa: BLE001 — fall through to next provider
            pass
    brave = os.environ.get("AIFORGE_BRAVE_API_KEY", "").strip()
    if brave:
        try:
            url = "https://api.search.brave.com/res/v1/web/search?" + \
                urllib.parse.urlencode({"q": query, "count": limit})
            req = urllib.request.Request(url, headers={
                "X-Subscription-Token": brave, "Accept": "application/json",
                "User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=_timeout(),
                                        context=_ssl_context_for("https://api.search.brave.com")) as r:
                d = _json.loads(r.read().decode("utf-8", "replace"))
            return [{"title": x.get("title", ""), "url": x.get("url", ""),
                     "snippet": x.get("description", "")}
                    for x in d.get("web", {}).get("results", [])][:limit]
        except Exception:  # noqa: BLE001
            pass
    return None


def web_search(args: dict, cwd: str | None = None) -> dict:
    """Search the open web. Uses a keyed provider (Tavily/Brave) when
    AIFORGE_TAVILY_API_KEY / AIFORGE_BRAVE_API_KEY is set — more reliable than
    HTML scraping — otherwise falls back to keyless DuckDuckGo. ``query``
    required, optional ``limit``."""
    if _disabled():
        return {"ok": False, "error": "web_search_disabled",
                "hint": "unset AIFORGE_WEB_SEARCH_DISABLE to enable"}
    query = (args.get("query") or args.get("q") or "").strip()
    if not query:
        return {"ok": False, "error": "missing 'query'"}
    limit = int(args.get("limit", _default_limit()))
    # `_api_search` returns None only when no key is set / the call FAILED; an
    # empty list is an authoritative "no results" from a configured provider, so
    # don't fall through to scraping DDG in that case.
    api = _api_search(query, limit)
    if api is not None:
        return {"ok": True, "query": query, "results": api, "provider": "api"}
    try:
        body = _get(_DDG_HTML, data=urllib.parse.urlencode(
            {"q": query, "kl": "us-en"}).encode())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            OSError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}

    # Pair each result link with the snippet that falls BETWEEN it and the
    # next result link — robust even when a block has no snippet (ads /
    # zero-click cards), unlike two independent findall lists that desync.
    matches = list(_RESULT_A.finditer(body))
    results: list[dict] = []
    for idx, m in enumerate(matches):
        title = _strip_tags(m.group(2))
        if not title:
            continue
        next_start = matches[idx + 1].start() if idx + 1 < len(matches) else len(body)
        sm = _SNIPPET.search(body, m.end(), next_start)
        results.append({
            "title": title,
            "url": _ddg_real_url(m.group(1)),
            "snippet": _strip_tags(sm.group(1)) if sm else "",
        })
        if len(results) >= limit:
            break
    if not results:
        return {"ok": True, "results": [], "provider": "ddg",
                "note": "no results (query too narrow, or DDG markup changed)"}
    return {"ok": True, "query": query, "results": results, "provider": "ddg"}


def web_fetch(args: dict, cwd: str | None = None) -> dict:
    """Fetch one web page and return its readable text (HTML tags, scripts and
    styles stripped). ``url`` required, optional ``max_chars`` (default 6000)."""
    if _disabled():
        return {"ok": False, "error": "web_search_disabled"}
    url = (args.get("url") or "").strip()
    if not url:
        return {"ok": False, "error": "missing 'url'"}
    if not re.match(r"^https?://", url, re.IGNORECASE):
        return {"ok": False, "error": "url must be http(s)"}
    try:
        max_chars = int(args.get("max_chars", _TEXT_DEFAULT))
    except (TypeError, ValueError):
        max_chars = _TEXT_DEFAULT
    try:
        raw = _get(url)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            OSError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}
    # Title, then body text with script/style/noscript removed.
    mt = re.search(r"<title[^>]*>(.*?)</title>", raw, re.DOTALL | re.IGNORECASE)
    title = _strip_tags(mt.group(1)) if mt else ""
    cleaned = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", raw,
                     flags=re.DOTALL | re.IGNORECASE)
    text = _strip_tags(cleaned)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()
    truncated = len(text) > max_chars
    return {"ok": True, "url": url, "title": title,
            "text": text[:max_chars], "truncated": truncated}


__all__ = ["web_search", "web_fetch"]
