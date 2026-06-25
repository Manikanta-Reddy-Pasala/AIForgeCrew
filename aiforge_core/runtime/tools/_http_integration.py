"""Shared HTTP plumbing for the Confluence / Jira / GitLab chat tools.

All three speak JSON over urllib with the same soft-error contract; only the
auth headers, base path and per-product error enrichment differ. This module
owns the common bits — truthiness, an insecure-TLS context, and the
request → parse → soft-error loop — so the tool modules don't triplicate them.

Each tool keeps its own ``_conf`` / ``_headers`` / ``_base`` (auth + URL shape)
and calls :func:`http_request` with a fully-built URL and headers.
"""
from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request

# Atlassian (Confluence / Jira DC) explains an auth denial in these response
# headers (CAPTCHA challenge, expired/invalid token, SSO, …). GitLab doesn't,
# so it passes ``capture_headers=()``.
ATLASSIAN_DENIED_HEADERS = (
    "X-Authentication-Denied-Reason", "WWW-Authenticate", "X-Seraph-LoginReason",
)


def truthy(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def ssl_context(insecure_tls: bool):
    """An unverified TLS context when ``insecure_tls`` (self-signed internal
    cert), else None (default verification)."""
    if insecure_tls:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return None


def http_request(method: str, url: str, *, headers: dict,
                 body: dict | None = None, timeout: int = 20,
                 body_cap: int = 200_000, context=None,
                 capture_headers: tuple[str, ...] = ()) -> dict:
    """Issue one JSON request and return the soft-error envelope.

    Returns ``{"ok": True, "data": <json|str|{}>}`` on success (an empty body —
    e.g. 204 No Content — yields ``data={}``), or ``{"ok": False, "error": …}``
    on any HTTP/transport error. On an HTTPError, ``detail`` carries the first
    500 chars of the body and any ``capture_headers`` present are surfaced as
    ``denied_reason``. Never raises.
    """
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=context) as r:
            raw = r.read(body_cap + 1)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read(2000).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            pass
        out = {"ok": False, "error": f"http {exc.code}", "detail": detail[:500]}
        for hk in capture_headers:
            try:
                hv = exc.headers.get(hk)
            except Exception:  # noqa: BLE001
                hv = None
            if hv:
                out.setdefault("denied_reason", f"{hk}: {hv}")
        return out
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}
    text = raw[:body_cap].decode("utf-8", "replace")
    if not text.strip():
        return {"ok": True, "data": {}}
    try:
        return {"ok": True, "data": json.loads(text)}
    except ValueError:
        return {"ok": True, "data": text}


__all__ = ["truthy", "ssl_context", "http_request", "ATLASSIAN_DENIED_HEADERS"]
