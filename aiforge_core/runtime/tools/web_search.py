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
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from aiforge_core.net.ssl import context_for as _ssl_context_for

_log = logging.getLogger("aiforge.web")

_DDG_HTML = "https://html.duckduckgo.com/html/"
_DDG_LITE = "https://lite.duckduckgo.com/lite/"   # simpler markup — fallback
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_FETCH_CAP = 1_000_000          # raw bytes read ceiling
_TEXT_DEFAULT = 6000            # default chars returned by web_fetch


def _disabled() -> bool:
    return str(os.environ.get("AIFORGE_WEB_SEARCH_DISABLE", "")).strip().lower() \
        in ("1", "true", "yes", "on")


def _fetch_allowed() -> bool:
    """Network lockdown: fetching an ARBITRARY URL (web_fetch) is off by
    default — same gate as doer_tools.fetch_url. web_SEARCH stays enabled
    (the researcher's sanctioned egress). Set AIFORGE_ALLOW_WEB_FETCH=1 to
    re-enable page fetching for the chat/other tools."""
    return str(os.environ.get("AIFORGE_ALLOW_WEB_FETCH", "0")).strip().lower() \
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


_NET_ERRORS = (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
               OSError, ValueError)


def _get(url: str, *, data: bytes | None = None,
         verified: "list | None" = None) -> str:
    """GET a page. On a CERTIFICATE failure — and only that — retry once
    without verification.

    A network that inspects TLS re-signs every response with a CA this process
    does not trust, so an ordinary public page dies with
    CERTIFICATE_VERIFY_FAILED and the agent is blind to the web. Verify first,
    so a page that CAN be fetched securely always is; fall back only on a cert
    error, and tell the caller through ``verified`` so the downgrade is
    reported rather than hidden. A refused connection or a timeout is not a
    cert problem and is never retried this way.
    """
    def _fetch(ctx) -> str:
        req = urllib.request.Request(
            url, data=data,
            headers={"User-Agent": _UA,
                     "Accept": "text/html,application/xhtml+xml",
                     "Accept-Language": "en-US,en;q=0.9"},
            method="POST" if data is not None else "GET")
        with urllib.request.urlopen(req, timeout=_timeout(), context=ctx) as r:
            raw = r.read(_FETCH_CAP + 1)
        # honour the response charset loosely; default utf-8
        return raw[:_FETCH_CAP].decode("utf-8", "replace")

    if verified is not None:
        verified.append(True)
    try:
        return _fetch(_ssl_context_for(url))
    except Exception as exc:  # noqa: BLE001 — classified immediately below
        from aiforge_core.net.ssl import (insecure_context, is_cert_error,
                                          web_tls_fallback_allowed_for)
        if not (is_cert_error(exc) and web_tls_fallback_allowed_for(url)):
            raise
        _log.warning(
            "web.tls_unverified url=%s — the certificate could not be "
            "verified (%s); refetching WITHOUT verification. Set "
            "AIFORGE_LLM_CA_BUNDLE to your network's CA to keep verifying, "
            "or AIFORGE_WEB_INSECURE_TLS=0 to fail instead.",
            url, str(exc)[:160])
        if verified is not None:
            verified[:] = [False]
        return _fetch(insecure_context())


def _get_retry(url: str, *, data: bytes | None = None, tries: int = 2,
               verified: "list | None" = None) -> str:
    """`_get` with a small retry — DDG's HTML endpoint intermittently 202s /
    rate-limits / drops the connection; one immediate retry recovers most of
    those transient misses. Raises the last error only after all tries fail."""
    last: Exception | None = None
    # ONE unverified refetch per chain, not one per attempt: `tries` x the
    # per-call fallback would re-send the same body unverified several times
    # over for a single logical request.
    _seen: list = verified if verified is not None else []
    for _ in range(max(1, tries)):
        try:
            return _get(url, data=data,
                        verified=None if _seen and _seen[0] is False else _seen)
        except _NET_ERRORS as exc:
            last = exc
    raise last if last else RuntimeError("request failed")


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
        # BOUNDED repetition, not possessive: the leading `[^>]` run must still
    # backtrack for the attribute after it to match, so `++` would break the
    # match outright. A bound caps the backtracking instead — and no real tag
    # attribute list approaches it. Input here is REMOTE HTML.
    r'<a[^>]{1,400}class="result__a"[^>]{1,400}href="([^"]{1,2000})"[^>]{0,400}>(.*?)</a>',
    re.DOTALL)
_SNIPPET = re.compile(
    r'class="result__snippet"[^>]*>(.*?)</a>', re.DOTALL)
# DDG lite (fallback): result links carry class="result-link".
_LITE_A = re.compile(
    r'<a\b[^>]*href="(https?://[^"]+)"[^>]*class=["\']result-link["\'][^>]*>'
    r'(.*?)</a>', re.DOTALL | re.IGNORECASE)


def _parse_html(body: str, limit: int) -> list[dict]:
    """Parse the html.duckduckgo.com result page (result__a links + the snippet
    that falls between each link and the next — robust to snippet-less cards)."""
    matches = list(_RESULT_A.finditer(body))
    results: list[dict] = []
    for idx, m in enumerate(matches):
        title = _strip_tags(m.group(2))
        if not title:
            continue
        nxt = matches[idx + 1].start() if idx + 1 < len(matches) else len(body)
        sm = _SNIPPET.search(body, m.end(), nxt)
        results.append({"title": title, "url": _ddg_real_url(m.group(1)),
                        "snippet": _strip_tags(sm.group(1)) if sm else ""})
        if len(results) >= limit:
            break
    return results


def _parse_lite(body: str, limit: int) -> list[dict]:
    """Parse the lite.duckduckgo.com result page (result-link anchors; lite has
    no reliable inline snippet, so snippet stays empty — the URL + title are
    what the agent needs to then web_fetch)."""
    results: list[dict] = []
    for m in _LITE_A.finditer(body):
        title = _strip_tags(m.group(2))
        if not title:
            continue
        results.append({"title": title, "url": _ddg_real_url(m.group(1)),
                        "snippet": ""})
        if len(results) >= limit:
            break
    return results


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

    # Keyless DDG scrape, HARDENED: try the html endpoint (with one retry), and
    # on any error OR zero parsed results fall back to the lite endpoint (simpler
    # markup that survives html-page A/B changes + rate-limit 202s). Only report
    # failure when BOTH endpoints come up empty/error.
    err: str | None = None
    for url, is_lite in ((_DDG_HTML, False), (_DDG_LITE, True)):
        _verified: list = []
        try:
            body = _get_retry(url, data=urllib.parse.urlencode(
                {"q": query, "kl": "us-en"}).encode(), verified=_verified)
        except _NET_ERRORS as exc:
            err = str(exc)
            continue
        results = _parse_lite(body, limit) if is_lite \
            else _parse_html(body, limit)
        if results:
            # Say it when the RESULT LIST itself came over an unverified
            # connection. These urls are what the agent fetches next, so an
            # attacker-substitutable result set reported as an ordinary success
            # is the worst place for this to be silent.
            return {"ok": True, "query": query, "results": results,
                    "provider": "ddg-lite" if is_lite else "ddg",
                    **({} if not (_verified and _verified[0] is False)
                       else {"tls_verified": False})}
    if err:
        return {"ok": False, "error": err, "query": query}
    return {"ok": True, "results": [], "provider": "ddg",
            "note": "no results (query too narrow, or DDG markup changed)"}


def web_fetch(args: dict, cwd: str | None = None) -> dict:
    """Fetch one web page and return its readable text (HTML tags, scripts and
    styles stripped). ``url`` required, optional ``max_chars`` (default 6000)."""
    if _disabled():
        return {"ok": False, "error": "web_search_disabled"}
    if not _fetch_allowed():
        return {"ok": False,
                "error": "web fetch disabled (set AIFORGE_ALLOW_WEB_FETCH=1)"}
    url = (args.get("url") or "").strip()
    if not url:
        return {"ok": False, "error": "missing 'url'"}
    if not re.match(r"^https?://", url, re.IGNORECASE):
        return {"ok": False, "error": "url must be http(s)"}
    try:
        max_chars = int(args.get("max_chars", _TEXT_DEFAULT))
    except (TypeError, ValueError):
        max_chars = _TEXT_DEFAULT
    return _fetch_readable(url, max_chars)


def _fetch_readable(url: str, max_chars: int) -> dict:
    """Gate-FREE readable-text fetch — the shared engine behind web_fetch and
    web_ingest's fallback (the researcher's sanctioned path must not re-hit
    the AIFORGE_ALLOW_WEB_FETCH gate web_fetch applies)."""
    # SSRF guard, matching web_ingest. This path took a model-supplied URL
    # straight to urlopen: it could reach cloud metadata, loopback services and
    # the private LAN. Adding the TLS fallback made that materially worse — an
    # internal HTTPS service used to fail closed on its self-signed cert — so
    # the guard belongs here regardless of the fallback's own host rule.
    from aiforge_core.net.ssl import SSRFBlocked, guard_public_url
    try:
        guard_public_url(url)
    except SSRFBlocked as exc:
        if exc.kind != "dns":       # a DNS failure is a normal network error
            return {"ok": False, "error": f"blocked (ssrf): {exc}"}
    _verified: list = []
    try:
        raw = _get(url, verified=_verified)
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}
    # Title, then body text with script/style/noscript removed.
    mt = re.search(r"<title[^>]*>(.*?)</title>", raw, re.DOTALL | re.IGNORECASE)
    title = _strip_tags(mt.group(1)) if mt else ""
    cleaned = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", raw,
                     flags=re.DOTALL | re.IGNORECASE)
    text = _strip_tags(cleaned)
    text = re.sub(r"[ \t]+", " ", text)
    # `\s` matches `\n`, so `\n\s*\n\s*\n+` can split one run of blank lines
    # many ways — quadratic on a long run. One unambiguous form: a newline
    # followed by two-or-more blank-ish lines.
    text = re.sub(r"\n(?:[ \t]*\n){2,}", "\n\n", text).strip()
    truncated = len(text) > max_chars
    out = {"ok": True, "url": url, "title": title,
           "text": text[:max_chars], "truncated": truncated}
    # Only ever stated when it is FALSE. A "tls_verified: true" on every
    # ordinary fetch is noise the model would carry in its context forever;
    # the exception is the thing worth knowing.
    if _verified and _verified[0] is False:
        out["tls_verified"] = False
    return out


__all__ = ["web_search", "web_fetch"]
