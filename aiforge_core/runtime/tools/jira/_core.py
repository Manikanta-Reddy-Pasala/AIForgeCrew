"""Jira config + HTTP foundation — env/stored config resolution, auth headers,
TLS context and the single ``_request`` helper every tool routes through.

Split out of the former ``jira.py`` module (grouped by concern); behaviour is
unchanged. The package ``__init__`` re-exports these names so ``jira.<name>``
attribute access is identical to before.
"""
from __future__ import annotations

import base64
import os
import urllib.parse

from aiforge_core.runtime.tools import _http_integration as _http

_TIMEOUT_S = 20
_BODY_CAP = 200_000

# Jira Server caps a single /search page (default 50, hard max ~100), so a bare
# maxResults=99 silently returns only the first page. We loop startAt in pages
# of this size until the caller's `limit` is met or the result set is exhausted.
_SEARCH_PAGE = 100

_truthy = _http.truthy


def _search_cap() -> int:
    """Hard ceiling on how many issues one search may pull (limit=all / 0).
    Guards against fetching an entire project. Tunable via env."""
    try:
        return max(1, int(os.environ.get("AIFORGE_JIRA_SEARCH_CAP", "500")))
    except ValueError:
        return 500


def _conf() -> dict:
    """Resolve config via the shared integration helper: base_url/token/
    insecure_tls (insecure by default) + user + default_project."""
    return _http.integration_conf(
        "jira", "JIRA",
        str_fields=(("user", "JIRA_USER"),
                    ("default_project", "JIRA_DEFAULT_PROJECT")))


def default_project() -> str:
    return _conf().get("default_project") or ""


def _auth_scheme() -> str:
    return "basic" if _conf()["user"] else "bearer"


def _base() -> str:
    return _conf()["base_url"]


def _configured() -> bool:
    c = _conf()
    return bool(c["base_url"] and c["token"])


def _headers() -> dict[str, str]:
    c = _conf()
    h = {"Content-Type": "application/json", "Accept": "application/json",
         "User-Agent": "AIForgeCrew-Jira/1.0"}
    if c["user"]:
        h["Authorization"] = "Basic " + base64.b64encode(
            f"{c['user']}:{c['token']}".encode()).decode()
    else:
        h["Authorization"] = "Bearer " + c["token"]
    return h


def _ssl_ctx():
    c = _conf()
    return _http.ssl_context(c["insecure_tls"], c.get("ca_bundle", ""),
                             c.get("base_url", ""))


def _request(method: str, path: str, *, params: dict | None = None,
             body: dict | None = None) -> dict:
    if not _configured():
        return {"ok": False, "error": "jira_not_configured",
                "hint": "set JIRA_BASE_URL + JIRA_TOKEN"}
    url = _base() + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return _http.http_request(method, url, headers=_headers(), body=body,
                              timeout=_TIMEOUT_S, body_cap=_BODY_CAP,
                              context=_ssl_ctx(),
                              capture_headers=_http.ATLASSIAN_DENIED_HEADERS)


def _issue_url(key: str) -> str:
    return f"{_base()}/browse/{key}" if key else ""
