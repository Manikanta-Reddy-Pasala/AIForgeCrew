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


def _crawl_gate(args: dict) -> dict | None:
    """Every reason this crawl must not happen, or None to proceed.

    ``sanctioned: True`` bypasses the AIFORGE_ALLOW_WEB_FETCH gate — set ONLY
    by the researcher wrapper (the role's sanctioned egress, parity with its
    ungated web_read); the chat path stays gated like web_fetch.

    SSRF guard: web_crawl is available to every agent (not just the
    researcher), so a model-supplied URL must not pivot to cloud metadata
    (169.254.169.254), loopback services or the private LAN — this applies to
    BOTH the crawl4ai browser engine and the plain-fetch fallback. A pure DNS
    failure is left to the engine to surface as a natural error.
    """
    from aiforge_core.runtime.tools import web_search as _ws
    if _ws._disabled():
        return {"ok": False, "error": "web_search_disabled"}
    if args.get("sanctioned") is not True and not _ws._fetch_allowed():
        return {"ok": False,
                "error": "web fetch disabled (set AIFORGE_ALLOW_WEB_FETCH=1)"}
    url = (args.get("url") or "").strip()
    if not url:
        return {"ok": False, "error": "missing 'url'"}
    if not re.match(r"^https?://", url, re.IGNORECASE):
        return {"ok": False, "error": "url must be http(s)"}
    from aiforge_core.net.ssl import SSRFBlocked, guard_public_url
    try:
        guard_public_url(url)
    except SSRFBlocked as exc:
        if exc.kind != "dns":
            return {"ok": False, "error": f"blocked (ssrf): {exc}"}
    return None


def _crawl4ai_text(url: str) -> tuple[str, str]:
    """``(text, title)`` from the browser engine, or empty on any failure —
    the plain fetch below still works."""
    if os.environ.get("AIFORGE_WEB_CRAWLER", "auto").strip().lower() == "fallback":
        return "", ""
    try:
        from aiforge_core.integrations import crawl4ai_adapter
        if not crawl4ai_adapter.available():
            return "", ""
        res = crawl4ai_adapter.crawl(url)
        return (res.get("markdown") or "").strip(), (res.get("title") or "")
    except Exception:  # noqa: BLE001
        return "", ""


def _fetch_page(url: str) -> tuple[dict, str, str, bool]:
    """``(error_or_empty, text, title, tls_verified)`` from the plain fetch.

    The TLS downgrade is carried through: this dossier is written so LATER
    SESSIONS reuse it, so a page fetched over an unverified connection that
    records nothing about it is the dangerous direction — the provenance is
    gone by the time anyone reads the note.
    """
    from aiforge_core.runtime.tools import web_search as _ws
    r = _ws._fetch_readable(url, 200_000)   # gate already applied by the caller
    if not r.get("ok"):
        return r, "", "", True
    return ({}, r.get("text") or "", r.get("title") or "",
            r.get("tls_verified") is not False)


def _write_dossier(url: str, title: str, text: str, engine: str,
                   tls_verified: bool) -> tuple[str, dict | None]:
    """Persist the page as a standard managed note. ``(page_path, error)``.

    The managed-note envelope (frontmatter + OKR sections) makes this dossier
    parseable/curatable like the jira/confluence ones; the full page text rides
    along untouched as the note body. The URL is sanitized — credentials and
    token values are never persisted.
    """
    from aiforge_core.runtime import work_context, work_notes
    safe_url = _sanitize_url(url)
    slug = _slug_for(url)
    ctx = work_context.context_dir("web", slug)
    page_path = os.path.join(ctx, "page.md")
    note = work_notes.render_note(
        "web", slug, title=title or safe_url, source_url=safe_url,
        facts=[f"title: {title or safe_url}", f"chars: {len(text)}",
               f"engine: {engine}"]
              + ([] if tls_verified else
                 ["tls: NOT VERIFIED (certificate could not be checked)"]),
        links=[safe_url], body_md=text)
    try:
        _atomic.write_text(page_path, note)
        _atomic.write_text(os.path.join(ctx, "meta.json"), json.dumps(
            {"url": safe_url, "title": title, "engine": engine,
             "fetched_at": int(time.time()), "chars": len(text),
             **({} if tls_verified else {"tls_verified": False})}, indent=1))
    except OSError as exc:
        return page_path, {"ok": False, "error": f"saved nothing: {exc}"}
    return page_path, None


def web_crawl(args: dict, cwd: str | None = None) -> dict:
    """Fetch ``url`` → markdown → ``work/web/<slug>/page.md``. Optional
    ``max_chars`` bounds the returned preview (full text is on disk).
    ``sanctioned: True`` bypasses the AIFORGE_ALLOW_WEB_FETCH gate — set
    ONLY by the researcher wrapper (the role's sanctioned egress, parity
    with its ungated web_read); the chat path stays gated like web_fetch."""
    if not isinstance(args, dict):
        return {"ok": False,
                "error": "missing 'url' (args must be a JSON object)"}
    blocked = _crawl_gate(args)
    if blocked is not None:
        return blocked
    url = (args.get("url") or "").strip()
    try:
        max_chars = int(args.get("max_chars", 3000))
    except (TypeError, ValueError):
        max_chars = 3000

    engine = "crawl4ai"
    tls_verified = True
    text, title = _crawl4ai_text(url)
    if not text:
        engine = "fetch"
        err, text, fetched_title, tls_verified = _fetch_page(url)
        if err:
            return err
        title = fetched_title or title
    if not text.strip():
        return {"ok": False, "error": "page fetched but no readable text"}

    page_path, write_err = _write_dossier(url, title, text, engine,
                                          tls_verified)
    if write_err is not None:
        return write_err
    return {"ok": True, "path": page_path, "url": _sanitize_url(url),
            "title": title, "engine": engine, "chars": len(text),
            **({} if tls_verified else {"tls_verified": False}),
            "preview": text[:max_chars],
            "note": "full page saved — file_read the path for more; "
                    "memory_write key take-aways you want recalled later"}


__all__ = ["web_crawl"]
