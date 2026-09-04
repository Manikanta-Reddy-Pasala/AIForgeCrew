"""HTTP fetch + crawl tools (fetch_url, web_read,
web_crawl) and their gate.

Split out of the former ``doer_tools`` module — moved verbatim.
"""
from __future__ import annotations

import logging
import os
import urllib.error
import urllib.request
from urllib.parse import urlsplit

_log = logging.getLogger("aiforge.web")


_FETCH_MAX_BYTES = 256 * 1024
def _fetch_timeout_s() -> float:
    """Same knob the chat fetcher reads. It was documented as "per-request"
    while three of the paths hardcoded their own number, so setting it to bound
    egress changed nothing here."""
    try:
        return float(os.environ.get("AIFORGE_WEB_TIMEOUT_S", "15"))
    except ValueError:
        return 15.0


def _open_web_response(req, url: str):
    """Open the GET, verified first. Falls back to an UNVERIFIED fetch only after
    the verified attempt fails with a certificate error on a network that
    inspects TLS (re-signs with an untrusted CA). Returns ``(resp_cm,
    unverified)``. AIFORGE_LLM_CA_BUNDLE is honoured on the verified attempt so
    an operator's installed CA actually takes effect."""
    from aiforge_core.net.ssl import (
        insecure_context,
        is_cert_error,
        public_verifying_context,
        web_tls_fallback_allowed_for,
    )
    try:
        return urllib.request.urlopen(req, timeout=_fetch_timeout_s(),
                                      context=public_verifying_context()), False
    except Exception as exc:  # noqa: BLE001 — classified right here
        if not (is_cert_error(exc) and web_tls_fallback_allowed_for(url)):
            raise
        _log.warning("web.tls_unverified url=%s err=%s — refetching PINNED to "
                     "the certificate that host presents (not verified to a "
                     "public root)", url, str(exc)[:160])
        return urllib.request.urlopen(req, timeout=_fetch_timeout_s(),
                                      context=insecure_context(url)), True


def _reguard_redirect(resp, url: str, guard_public_url, ssrf_blocked) -> dict | None:
    """Re-guard the final URL after any redirect hops — a public URL can 30x to
    a private/metadata target, or to a search engine. Returns a refusal dict,
    or None to allow.

    NOTE the limit, and do not overstate it in docs: urlopen has already
    followed the hops by the time we see ``resp``, so the request (and its query
    string) is on the wire. This refuses the RESULT; it does not prevent the
    call. Only web_fetch's own opener guards each hop before it is made.
    """
    final = getattr(resp, "url", None)
    if final and final != url:
        from aiforge_core.net import egress as _egress
        if _egress.looks_like_search(final):
            return {"ok": False, "error": "web_search_removed",
                    "hint": ("redirected to a search engine; this install has "
                             "no web search.")}
        if not _egress.host_allowed(final):
            return {"ok": False, "error": "host_not_allowed",
                    "hint": (f"redirected to {final[:120]}, which is not on the "
                             "egress allowlist.")}
        try:
            guard_public_url(final)
        except ssrf_blocked as exc:
            if exc.kind != "dns":
                return {"ok": False,
                        "error": f"blocked after redirect (ssrf): {exc}"}
    return None


def _do_fetch(url: str) -> dict:
    """The actual http(s) GET (no gate). NOT a browser: no cookies, no JS,
    no redirects to file://. Body capped at 256 KB, timeout 15s, http(s)
    only. Used by the gated ``fetch_url`` and the researcher-only ``web_read``."""
    # SCHEME ONLY here, deliberately. url_policy's https requirement is for
    # endpoints we send API keys and prompt text to; this reads PUBLIC PAGES:
    # much of the web is still http, nothing of ours goes with the request, and
    # the SSRF guard below is what actually matters.
    if not url or not str(url).lower().startswith(("http://", "https://")):
        return {"ok": False, "error": "url must be http(s)"}
    # SSRF guard: a model-supplied URL must not pivot to cloud metadata
    # (169.254.169.254), loopback services or the private LAN. A pure DNS failure
    # is left to urlopen to surface as a natural network error.
    from aiforge_core.net.ssl import SSRFBlocked, guard_public_url
    try:
        guard_public_url(url)
    except SSRFBlocked as exc:
        if exc.kind != "dns":
            return {"ok": False, "error": f"blocked (ssrf): {exc}"}

    # A plain http:// page arrives over a channel nobody authenticated — the
    # same fact the broken-TLS-chain path already reports. Say so with the same
    # field rather than inventing a second vocabulary for one meaning: the
    # model is about to act on this content, and "it could have been tampered
    # with in transit" is what it needs to know.
    unverified = urlsplit(str(url)).scheme.lower() == "http"
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "AIForgeCrew-Doer/1.0"})
        resp_cm, downgraded = _open_web_response(req, url)
        unverified = unverified or downgraded
        with resp_cm as resp:
            blocked = _reguard_redirect(resp, url, guard_public_url, SSRFBlocked)
            if blocked is not None:
                return blocked
            raw = resp.read(_FETCH_MAX_BYTES + 1)
            status = resp.status
            ctype = resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        return {"ok": False, "error": f"http {exc.code}", "status": exc.code}
    except urllib.error.URLError as exc:
        return {"ok": False, "error": f"url error: {exc.reason}"}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}

    truncated = len(raw) > _FETCH_MAX_BYTES
    return {
        "ok": True, "url": url, "status": status, "content_type": ctype,
        "body": raw[:_FETCH_MAX_BYTES].decode("utf-8", "replace"),
        "bytes": len(raw), "truncated": truncated,
        # Stated only when the answer is "no" — an ordinary verified fetch should
        # not carry a reassurance the model then repeats forever.
        **({"tls_verified": False} if unverified else {}),
    }


def fetch_url(url: str) -> dict:
    """GET an http(s) URL and return the body as text.

    Gated behind ``AIFORGE_ALLOW_WEB_FETCH`` (default off) and the
    ``AIFORGE_WEB_FETCH_DISABLE`` hard-off. A search engine's result URL is
    refused whatever the switches say — see aiforge_core.net.egress."""
    from aiforge_core.net import egress as _egress
    refusal = _egress.check(url)
    if refusal is not None:
        return refusal
    return _do_fetch(url)


def web_read(url: str) -> dict:
    """Fetch + return the text of a web page. RESEARCHER-only reader — the one
    agent allowed to read a page it was pointed at (other roles never get this
    schema). Same 256 KB / 15s / http(s)-only limits.

    It used to be UNGATED, on the reasoning that the researcher's search→read
    flow would otherwise be dead. Search is gone, so that reasoning is gone
    with it: an ungated reader on the one role that runs unattended, before any
    human sees the ticket, is simply the widest egress in the system. It now
    answers to the same switches as everything else."""
    from aiforge_core.net import egress as _egress
    refusal = _egress.check(url)
    if refusal is not None:
        return refusal
    return _do_fetch(url)


def web_crawl(url: str, max_chars: int = 3000) -> dict:
    """Fetch a page as clean markdown AND save it to the shared
    work/web/<slug>/ dossier for reuse across sessions — prefer this over
    web_read when the page is documentation worth keeping.

    This wrapper used to pass ``sanctioned: True``, which bypassed the fetch
    gate — and since web_crawl is in the BASE tool list, that bypass applied to
    every role, not the researcher it was written for. It is gone: web_crawl
    now obeys AIFORGE_ALLOW_WEB_FETCH like every other page read."""
    try:
        from aiforge_core.runtime.tools import web_ingest as _wi
        return _wi.web_crawl({"url": url, "max_chars": int(max_chars or 3000)})
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
