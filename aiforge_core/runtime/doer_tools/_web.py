"""HTTP fetch + web search/crawl tools (fetch_url, web_read, web_search,
web_crawl) and their gate.

Split out of the former ``doer_tools`` module — moved verbatim.
"""
from __future__ import annotations

import logging
import os
import urllib.error
import urllib.request

_log = logging.getLogger("aiforge.web")


_FETCH_MAX_BYTES = 256 * 1024
_FETCH_TIMEOUT_S = 15


def _web_fetch_allowed() -> bool:
    """Network+telemetry lockdown: arbitrary-URL fetch is OFF by default.

    The only sanctioned agent egress is the researcher's ``web_search``
    (its own gate) plus the configured LLM endpoint. Set
    ``AIFORGE_ALLOW_WEB_FETCH=1`` to re-enable arbitrary fetch/http_get.
    """
    return str(os.environ.get("AIFORGE_ALLOW_WEB_FETCH", "0")).strip().lower() in (
        "1", "true", "yes", "on",
    )


def _open_web_response(req, url: str):
    """Open the GET, verified first. Falls back to an UNVERIFIED fetch only after
    the verified attempt fails with a certificate error on a network that
    inspects TLS (re-signs with an untrusted CA). Returns ``(resp_cm,
    unverified)``. AIFORGE_LLM_CA_BUNDLE is honoured on the verified attempt so
    an operator's installed CA actually takes effect."""
    from aiforge_core.net.ssl import (insecure_context, is_cert_error,
                                      public_verifying_context,
                                      web_tls_fallback_allowed_for)
    try:
        return urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT_S,
                                      context=public_verifying_context()), False
    except Exception as exc:  # noqa: BLE001 — classified right here
        if not (is_cert_error(exc) and web_tls_fallback_allowed_for(url)):
            raise
        _log.warning("web.tls_unverified url=%s err=%s — refetching without "
                     "verification", url, str(exc)[:160])
        return urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT_S,
                                      context=insecure_context()), True


def _reguard_redirect(resp, url: str, guard_public_url, SSRFBlocked) -> "dict | None":
    """Re-guard the final URL after any redirect hops — a public URL can 30x to
    a private/metadata target. Returns a refusal dict, or None to allow."""
    final = getattr(resp, "url", None)
    if final and final != url:
        try:
            guard_public_url(final)
        except SSRFBlocked as exc:
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

    unverified = False
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "AIForgeCrew-Doer/1.0"})
        resp_cm, unverified = _open_web_response(req, url)
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

    Gated behind ``AIFORGE_ALLOW_WEB_FETCH`` (default off) — under the
    network lockdown, general agents cannot fetch arbitrary URLs. The
    RESEARCHER uses the ungated ``web_read`` instead (role-scoped web)."""
    if not _web_fetch_allowed():
        return {"ok": False, "error": "web fetch disabled (set AIFORGE_ALLOW_WEB_FETCH=1)"}
    return _do_fetch(url)


def web_read(url: str) -> dict:
    """Fetch + return the text of a web page. RESEARCHER-only sanctioned
    reader — the one agent allowed to READ a page it found via web_search.
    Ungated on purpose (only the researcher's tool set receives it; other
    agents never get this schema). Same 256 KB / 15s / http(s)-only limits."""
    return _do_fetch(url)


def web_search(query: str, k: int = 5) -> dict:
    """Search the open web (DuckDuckGo, no API key) when you're stuck — an
    unfamiliar error, a library API you can't recall, a config flag. Returns
    ranked {title, url, snippet}; follow up with fetch_url on the best hit."""
    try:
        from aiforge_core.runtime.tools import web_search as _ws
        return _ws.web_search({"query": query, "limit": int(k or 5)})
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def web_crawl(url: str, max_chars: int = 3000) -> dict:
    """Fetch a page as clean markdown AND save it to the shared
    work/web/<slug>/ dossier for reuse across sessions — prefer this over
    web_read when the page is documentation worth keeping. Researcher-only
    tool, so it rides the role's sanctioned egress (parity with the ungated
    web_read — gating it on AIFORGE_ALLOW_WEB_FETCH would make it dead on
    arrival for the ONE agent that receives it)."""
    try:
        from aiforge_core.runtime.tools import web_ingest as _wi
        return _wi.web_crawl({"url": url, "max_chars": int(max_chars or 3000),
                              "sanctioned": True})
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
