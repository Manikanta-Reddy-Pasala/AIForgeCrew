"""Web page fetch for the agent. NO SEARCH.

The agent can read ONE page it was pointed at. It cannot query a search
engine: web SEARCH was removed deliberately (2026-09-03) because the query
string is outbound data — whatever the model typed (an error from your logs,
a symbol name, a customer name) left the box to a third-party engine, and
nothing filtered it. Fetching a URL the user supplied is a different risk: the
destination is known, and it is off by default anyway.

Do not reintroduce a search backend here. If a lookup is genuinely needed, the
user pastes the URL and the agent reads it.

Tools:
  web_fetch(url, max_chars) -> readable text of one page (tags stripped)

Knobs (env, all optional):
  AIFORGE_ALLOW_WEB_FETCH=1      allow fetching a URL at all (default OFF)
  AIFORGE_WEB_FETCH_DISABLE=1    hard-off, even when the above is set
                                 (AIFORGE_WEB_SEARCH_DISABLE is still honoured
                                 so an existing deployment that set it stays
                                 locked down)
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
import urllib.request

from aiforge_core.net import egress as _egress
from aiforge_core.net.ssl import context_for as _ssl_context_for

_log = logging.getLogger("aiforge.web")

_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_FETCH_CAP = 1_000_000          # raw bytes read ceiling
_TEXT_DEFAULT = 6000            # default chars returned by web_fetch


# Both gates live in aiforge_core.net.egress so every outbound path — chat,
# doer, researcher, crawler, browser — answers to the same two switches. These
# names are kept because callers and tests already use them.
_disabled = _egress.hard_off
_fetch_allowed = _egress.fetch_allowed


def _timeout() -> float:
    try:
        return float(os.environ.get("AIFORGE_WEB_TIMEOUT_S", "12"))
    except ValueError:
        return 12.0


class _GuardedRedirect(urllib.request.HTTPRedirectHandler):
    """Re-run the guards on every redirect hop.

    urlopen follows redirects itself, so guarding only the URL we were handed
    checks the one hop an attacker controls least: ``https://ok.example/x`` can
    302 to ``http://169.254.169.254/`` (cloud credentials) or to a search
    engine, and the body comes back as an ordinary success. This raises
    instead, and the caller reports it like any other fetch error.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        from aiforge_core.net.ssl import SSRFBlocked, guard_public_url

        def _refuse(reason: str):
            # urllib closes `fp` only AFTER this returns, so raising out of
            # here leaks the socket of every refused redirect.
            try:
                if fp is not None:
                    fp.close()
            except Exception:  # noqa: BLE001 — best-effort
                pass
            return urllib.error.URLError(reason)

        if _egress.looks_like_search(newurl):
            raise _refuse(
                "redirect to a search engine refused: web search was removed")
        # SSRF FIRST, so a metadata/private target is refused with the reason
        # that actually matters. Ordering the allowlist ahead of it reported
        # 169.254.169.254 as "not on your allowlist", which reads like a
        # configuration gap and sends the next person after the wrong problem.
        try:
            guard_public_url(newurl)
        except SSRFBlocked as exc:
            if exc.kind != "dns":   # DNS failure surfaces as a normal error
                raise _refuse(f"blocked after redirect (ssrf): {exc}") from exc
        # Then the ALLOWLIST, on every hop. Checking only the URL we were handed
        # makes a one-line open redirect on an allowed host into a fetch of
        # anywhere: docs.example/r?to=evil.example passed, 302'd, and the body
        # came back ok:True from a host that was never on the list.
        if not _egress.host_allowed(newurl):
            raise _refuse(
                f"redirect to a host that is not on the egress allowlist: "
                f"{newurl[:120]}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _opener(ctx):
    """One opener per call: the TLS context differs between the verified and
    the fallback attempt, and an opener caches its handlers."""
    return urllib.request.build_opener(
        _GuardedRedirect(), urllib.request.HTTPSHandler(context=ctx))


def _get(url: str, *, verified: list | None = None) -> str:
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
        # GET only, no body: this module reads a page, it never posts one.
        req = urllib.request.Request(
            url,
            headers={"User-Agent": _UA,
                     "Accept": "text/html,application/xhtml+xml",
                     "Accept-Language": "en-US,en;q=0.9"},
            method="GET")
        with _opener(ctx).open(req, timeout=_timeout()) as r:
            raw = r.read(_FETCH_CAP + 1)
        # honour the response charset loosely; default utf-8
        return raw[:_FETCH_CAP].decode("utf-8", "replace")

    if verified is not None:
        verified.append(True)
    try:
        return _fetch(_ssl_context_for(url))
    except Exception as exc:  # noqa: BLE001 — classified immediately below
        from aiforge_core.net.ssl import (
            insecure_context,
            is_cert_error,
            web_tls_fallback_allowed_for,
        )
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


def _strip_tags(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s).strip()


def web_fetch(args: dict, _cwd: str | None = None) -> dict:
    """Fetch one web page and return its readable text (HTML tags, scripts and
    styles stripped). ``url`` required, optional ``max_chars`` (default 6000)."""
    url = (args.get("url") or "").strip()
    if not url:
        return {"ok": False, "error": "missing 'url'"}
    if not re.match(r"^https?://", url, re.IGNORECASE):
        return {"ok": False, "error": "url must be http(s)"}
    refusal = _egress.check(url)
    if refusal is not None:
        return refusal
    try:
        max_chars = int(args.get("max_chars", _TEXT_DEFAULT))
    except (TypeError, ValueError):
        max_chars = _TEXT_DEFAULT
    return _fetch_readable(url, max_chars)


def _fetch_readable(url: str, max_chars: int) -> dict:
    """Readable-text fetch — the shared engine behind web_fetch and
    web_ingest's fallback. The on/off SWITCHES are the caller's business (it
    knows which tool is asking), but the search-engine refusal is not: it must
    hold on every path or the removed capability comes back through whichever
    caller forgot."""
    if _egress.looks_like_search(url):
        return {"ok": False, "error": "web_search_removed",
                "hint": ("this install has no web search — fetching a search "
                         "engine's result page is the same thing.")}
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


__all__ = ["web_fetch"]
