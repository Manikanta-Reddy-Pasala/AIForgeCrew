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
import os
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


def integration_conf(name: str, env_prefix: str, *,
                     str_fields: "tuple[tuple[str, str], ...]" = (),
                     bool_fields: "tuple[tuple[str, str], ...]" = ()) -> dict:
    """Resolve a jira/confluence/gitlab config: env var WINS, else the
    UI/chat-persisted store. Shared so the three tools don't triplicate the
    env-or-store + token-strip + insecure-TLS-by-default logic.

    Always returns ``base_url`` (trailing slash stripped), ``token`` (whitespace
    stripped — a stray newline in an auth header yields a 401), ``ca_bundle``
    and ``insecure_tls``.

    ``insecure_tls`` DEFAULTS TO TRUE: these integrations hit internal, often
    self-signed, endpoints and the UI toggle was removed, so an unset
    ``{PREFIX}_INSECURE_TLS`` skips verification. **Know what that costs** — the
    integration's bearer token then travels over a connection whose certificate
    nobody checked, so an on-path attacker on the route to your Jira can take
    it. Prefer ``{PREFIX}_CA_BUNDLE`` (or ``AIFORGE_CA_BUNDLE``): verification
    stays ON, anchored to the internal CA, which handles the self-signed case
    without the exposure. ``{PREFIX}_INSECURE_TLS=0`` also forces verification.

    ``str_fields`` = extra ``(key, ENV)`` string fields (e.g.
    ``("default_project", "JIRA_DEFAULT_PROJECT")``); ``bool_fields`` = extra
    ``(key, ENV)`` booleans (env truthy OR stored truthy, e.g. gitlab ``oauth``).
    """
    try:
        from aiforge_core.config import integrations
        stored = integrations.get(name)
    except Exception:  # noqa: BLE001
        stored = {}

    def _s(key: str, env: str) -> str:
        return (os.environ.get(env) or stored.get(key) or "").strip()

    _ins = os.environ.get(f"{env_prefix}_INSECURE_TLS")
    conf = {
        "base_url": _s("base_url", f"{env_prefix}_BASE_URL").rstrip("/"),
        "token": _s("token", f"{env_prefix}_TOKEN"),
        # Unset means SKIP VERIFY (operator's call, restored deliberately):
        # these integrations point at internal, often self-signed, endpoints
        # and the UI toggle for this was removed. `{PREFIX}_INSECURE_TLS=0`
        # turns verification on, and `{PREFIX}_CA_BUNDLE` is the better answer
        # — it keeps verification ON and anchors it to the internal CA.
        "insecure_tls": _ins is None or truthy(_ins),
        "ca_bundle": (_s("ca_bundle", f"{env_prefix}_CA_BUNDLE")
                      or os.environ.get("AIFORGE_CA_BUNDLE", "").strip()),
    }
    for key, env in str_fields:
        conf[key] = _s(key, env)
    for key, env in bool_fields:
        conf[key] = truthy(os.environ.get(env, "")) or bool(stored.get(key))
    return conf


def ssl_context(insecure_tls: bool, ca_bundle: str = "", url: str = ""):
    """The TLS context for an integration call, in order of preference:

    * ``ca_bundle`` set → verification stays ON, anchored to that CA. The right
      answer for a self-signed internal Jira/Confluence/GitLab when the
      operator has the internal CA to hand.
    * ``insecure_tls`` → verification stays ON, anchored to THAT ENDPOINT'S own
      certificate, pinned on first use (``net.trust``). It used to mean
      CERT_NONE, which sent the integration's auth token over a connection
      nobody had authenticated — the flag meant "this host is self-signed", and
      it now does what that should have meant all along.
    * otherwise → None, meaning urllib's own default verification.
    """
    if ca_bundle:
        try:
            return ssl.create_default_context(cafile=ca_bundle)
        except (OSError, ssl.SSLError) as exc:
            # A bad path must not silently downgrade to "no verification".
            raise ValueError(
                f"CA bundle {ca_bundle!r} could not be loaded: {exc}") from exc
    if insecure_tls:
        from aiforge_core.net import ssl as _nssl
        return _nssl.insecure_context(url or None)
    return None


def _http_error_envelope(exc: "urllib.error.HTTPError",
                         capture_headers: tuple) -> dict:
    """The soft-error envelope for an HTTPError — ``detail`` is the first 500
    chars of the body, and any ``capture_headers`` present surface as
    ``denied_reason``."""
    detail = ""
    try:
        detail = exc.read(2000).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        pass
    out = {"ok": False, "error": f"http {exc.code}", "detail": detail[:500]}
    hdrs = getattr(exc, "headers", None)
    if hdrs is not None:
        for hk in capture_headers:
            hv = hdrs.get(hk)
            if hv:
                out.setdefault("denied_reason", f"{hk}: {hv}")
    return out


def _http_success_envelope(raw: bytes, body_cap: int, parse_json: bool) -> dict:
    """The success envelope. ``read(body_cap + 1)`` is one byte past the cap so
    truncation is DETECTABLE downstream (a caller taking ``data[-n:]`` believing
    it had the end of a file otherwise got the middle with no way to know)."""
    truncated = len(raw) > body_cap
    text = raw[:body_cap].decode("utf-8", "replace")
    out = {"ok": True, "data": text}
    if truncated:
        # The CAP, in bytes — named for what it holds ("truncated_bytes" would
        # read as a count of dropped bytes, which nobody here knows).
        out["body_cap_hit"] = body_cap
    if not parse_json or not text.strip():
        if parse_json and not text.strip():
            out["data"] = {}       # empty body (e.g. 204) → {}
        return out
    try:
        out["data"] = json.loads(text)
    except ValueError:
        pass
    return out


def http_request(method: str, url: str, *, headers: dict,
                 body: dict | None = None, timeout: int = 20,
                 body_cap: int = 200_000, context=None,
                 capture_headers: tuple[str, ...] = (),
                 parse_json: bool = True) -> dict:
    """Issue one JSON request and return the soft-error envelope.

    Returns ``{"ok": True, "data": <json|str|{}>}`` on success (an empty body —
    e.g. 204 No Content — yields ``data={}``), or ``{"ok": False, "error": …}``
    on any HTTP/transport error. A non-JSON body that hit ``body_cap`` also
    carries ``body_cap_hit`` — the CAP that was reached, not a count of dropped
    bytes.

    ``parse_json=False`` returns the body as text untouched. Endpoints that serve
    plain text (a CI job log) must use it: the speculative ``json.loads`` turns a
    log that happens to BE json into a dict, and an empty body into ``{}``. Never
    raises.
    """
    # EGRESS POLICY. This is the single entry point for Jira, Confluence and
    # GitLab, so one check here covers all three — including the case the
    # approval gate never covered: an UNATTENDED run (ticket pipeline, cron
    # job) has no approver, so tool_gate degrades ASK to allow and a write
    # would go out with nobody watching.
    from aiforge_core.net import egress as _egress
    refusal = _egress.allow("integration", url, method=method)
    if refusal is not None:
        return refusal
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=context) as r:
            raw = r.read(body_cap + 1)
    except urllib.error.HTTPError as exc:
        return _http_error_envelope(exc, capture_headers)
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}
    return _http_success_envelope(raw, body_cap, parse_json)


class _StripAuthOnCrossHostRedirect(urllib.request.HTTPRedirectHandler):
    """A 3xx that crosses hosts must NOT carry the Authorization header — the
    default opener re-sends it everywhere, leaking the Bearer/PAT token to a
    redirect target (a poisoned attachment URL → credential exfiltration)."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            try:
                oh = urllib.parse.urlsplit(req.full_url).hostname
                nh = urllib.parse.urlsplit(newurl).hostname
                if oh != nh:
                    for store in (new.headers, getattr(new, "unredirected_hdrs", {})):
                        for k in [k for k in store if k.lower() == "authorization"]:
                            store.pop(k, None)
            except Exception:  # noqa: BLE001
                pass
        return new


def http_get_bytes(url: str, *, headers: dict, timeout: int = 20,
                   cap: int = 5 * 1024 * 1024, context=None,
                   allow_host: str | None = None) -> dict:
    """Fetch raw bytes (e.g. an image attachment). Returns ``{ok, bytes}`` or
    ``{ok: False, error}``. Caps the body, strips auth on cross-host redirects,
    and (when ``allow_host`` is given) refuses a URL on a different host than the
    configured integration — so a model-supplied/poisoned attachment URL can't
    SSRF an internal host or exfiltrate the token. Never raises."""
    if allow_host:
        try:
            h = urllib.parse.urlsplit(url).hostname
            if h and h.lower() != allow_host.lower():
                return {"ok": False, "error": "host_not_allowed", "host": h}
        except Exception:  # noqa: BLE001
            return {"ok": False, "error": "bad_url"}
    # ...and the egress policy regardless. `allow_host` is OPTIONAL, so it
    # protects only the callers that remember it — the next one that forgets
    # fetches an attacker's URL with the integration's Authorization header
    # attached. This check does not depend on the caller being careful.
    from aiforge_core.net import egress as _egress
    _ref = _egress.allow("integration", url)
    if _ref is not None:
        return _ref
    req = urllib.request.Request(url, headers=headers, method="GET")
    handlers = [_StripAuthOnCrossHostRedirect()]
    if context is not None:
        handlers.append(urllib.request.HTTPSHandler(context=context))
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(req, timeout=timeout) as r:
            raw = r.read(cap + 1)
    except urllib.error.HTTPError as exc:
        return {"ok": False, "error": f"http {exc.code}"}
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}
    if len(raw) > cap:
        return {"ok": False, "error": "too_large", "limit": cap}
    return {"ok": True, "bytes": raw}


__all__ = ["truthy", "ssl_context", "http_request", "http_get_bytes",
           "ATLASSIAN_DENIED_HEADERS"]
