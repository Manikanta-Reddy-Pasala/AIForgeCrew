"""web_crawl — fetch a URL as clean markdown and file it as a WEB dossier.

Domain tool (separation of concerns): the crawl4ai LIBRARY lives behind
``aiforge_core.integrations.crawl4ai_adapter``; this module owns policy —
egress gate, url validation, engine fallback, dossier layout.

Engine order:
  1. crawl4ai adapter (optional extra ``crawl``): headless-browser render →
     real markdown. Best for JS-heavy doc sites.
  2. plain fetch fallback (always available): the same tag-strip fetch
     ``web_fetch`` uses — no new deps, degraded but functional.

The page lands in the shared workspace ``~/.aiforge/work/web/<slug>/page.md``
(+ ``meta.json``) so later sessions reuse it, mirroring the jira/confluence
dossier pattern. Same egress gate as web_fetch: ``AIFORGE_ALLOW_WEB_FETCH=1``.

Soft-error contract: returns ``{"ok": bool, ...}``, never raises.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.parse

from aiforge_core.config import _atomic

# query keys whose VALUES are secrets — redacted before anything is persisted
_SECRET_QS_RE = re.compile(r"token|key|secret|password|passwd|auth|sig|cred",
                           re.IGNORECASE)


def _sanitize_url(url: str) -> str:
    """Strip userinfo (user:pass@) and redact secret-looking query values —
    the dossier is a SHARED on-disk workspace read by future sessions, so a
    credentialed URL must never be persisted verbatim."""
    p = urllib.parse.urlparse(url)
    host = p.netloc.rsplit("@", 1)[-1]          # drop user:pass@
    q = [(k, "REDACTED" if _SECRET_QS_RE.search(k) else v)
         for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True)]
    return urllib.parse.urlunparse(
        (p.scheme, host, p.path, p.params, urllib.parse.urlencode(q), ""))


def _slug_for(url: str) -> str:
    """Human-readable prefix + a short hash of the FULL sanitized URL — the
    hash keeps distinct pages distinct (query strings, >80-char paths, case)
    instead of silently overwriting each other's dossier."""
    clean = _sanitize_url(url)
    p = urllib.parse.urlparse(clean)
    base = (p.netloc + "-" + p.path.strip("/").replace("/", "-"))[:70]
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", base).strip("-").lower() or "page"
    return f"{s}-{hashlib.sha1(clean.encode('utf-8')).hexdigest()[:8]}"


def web_crawl(args: dict, cwd: str | None = None) -> dict:
    """Fetch ``url`` → markdown → ``work/web/<slug>/page.md``. Optional
    ``max_chars`` bounds the returned preview (full text is on disk).
    ``sanctioned: True`` bypasses the AIFORGE_ALLOW_WEB_FETCH gate — set
    ONLY by the researcher wrapper (the role's sanctioned egress, parity
    with its ungated web_read); the chat path stays gated like web_fetch."""
    if not isinstance(args, dict):
        return {"ok": False, "error": "missing 'url' (args must be a JSON object)"}
    from aiforge_core.runtime.tools import web_search as _ws
    if _ws._disabled():
        return {"ok": False, "error": "web_search_disabled"}
    sanctioned = args.get("sanctioned") is True
    if not sanctioned and not _ws._fetch_allowed():
        return {"ok": False,
                "error": "web fetch disabled (set AIFORGE_ALLOW_WEB_FETCH=1)"}
    url = (args.get("url") or "").strip()
    if not url:
        return {"ok": False, "error": "missing 'url'"}
    if not re.match(r"^https?://", url, re.IGNORECASE):
        return {"ok": False, "error": "url must be http(s)"}
    # SSRF guard: web_crawl is now available to every agent (not just the
    # researcher), so a model-supplied URL must not pivot to cloud metadata
    # (169.254.169.254), loopback services or the private LAN — applies to
    # BOTH the crawl4ai browser engine and the plain-fetch fallback below. A
    # pure DNS failure is left to the engine to surface as a natural error.
    from aiforge_core.net.ssl import SSRFBlocked, guard_public_url
    try:
        guard_public_url(url)
    except SSRFBlocked as exc:
        if exc.kind != "dns":
            return {"ok": False, "error": f"blocked (ssrf): {exc}"}
    try:
        max_chars = int(args.get("max_chars", 3000))
    except (TypeError, ValueError):
        max_chars = 3000

    engine = "crawl4ai"
    title = ""
    text = ""
    tls_verified = True
    prefer = os.environ.get("AIFORGE_WEB_CRAWLER", "auto").strip().lower()
    if prefer != "fallback":
        try:
            from aiforge_core.integrations import crawl4ai_adapter
            if crawl4ai_adapter.available():
                res = crawl4ai_adapter.crawl(url)
                text = (res.get("markdown") or "").strip()
                title = res.get("title") or ""
        except Exception:  # noqa: BLE001 — plain fetch below still works
            text = ""
    if not text:
        engine = "fetch"
        r = _ws._fetch_readable(url, 200_000)   # gate already applied above
        if not r.get("ok"):
            return r
        text = r.get("text") or ""
        title = r.get("title") or title
        # Carry the TLS downgrade through. This dossier is written so LATER
        # SESSIONS reuse it, so a page fetched over an unverified connection
        # that records nothing about it is the dangerous direction: the
        # provenance is gone by the time anyone reads the note.
        if r.get("tls_verified") is False:
            tls_verified = False
    if not text.strip():
        return {"ok": False, "error": "page fetched but no readable text"}

    from aiforge_core.runtime import work_context, work_notes
    safe_url = _sanitize_url(url)   # never persist credentials/token values
    slug = _slug_for(url)
    ctx = work_context.context_dir("web", slug)
    page_path = os.path.join(ctx, "page.md")
    meta_path = os.path.join(ctx, "meta.json")
    # Standard managed-note envelope (frontmatter + OKR sections) so this
    # dossier is parseable/curatable like the jira/confluence ones; the full
    # page text rides along untouched as the note body.
    note = work_notes.render_note(
        "web", slug, title=title or safe_url, source_url=safe_url,
        facts=[f"title: {title or safe_url}", f"chars: {len(text)}",
               f"engine: {engine}"]
              + ([] if tls_verified else
                 ["tls: NOT VERIFIED (certificate could not be checked)"]),
        links=[safe_url], body_md=text)
    try:
        _atomic.write_text(page_path, note)
        _atomic.write_text(meta_path, json.dumps(
            {"url": safe_url, "title": title, "engine": engine,
             "fetched_at": int(time.time()), "chars": len(text),
             **({} if tls_verified else {"tls_verified": False})}, indent=1))
    except OSError as exc:
        return {"ok": False, "error": f"saved nothing: {exc}"}
    return {"ok": True, "path": page_path, "url": safe_url, "title": title,
            "engine": engine, "chars": len(text),
            **({} if tls_verified else {"tls_verified": False}),
            "preview": text[:max_chars],
            "note": "full page saved — file_read the path for more; "
                    "memory_write key take-aways you want recalled later"}


__all__ = ["web_crawl"]
