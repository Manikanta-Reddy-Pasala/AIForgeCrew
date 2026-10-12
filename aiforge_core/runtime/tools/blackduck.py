"""Black Duck tool — read what a scan found: policy status, the components
that break policy, and the open vulnerabilities.

Read-only. The agent gets a Black Duck link (Detect prints one in the CI job
log) or a project name and version, and this answers through the REST API.

Config (env, or Settings → Integrations):
  BLACKDUCK_BASE_URL   e.g. https://blackduck.internal  (no trailing /)
  BLACKDUCK_TOKEN      a user API token (My Access Tokens). Read access to
                       the project is enough.
  BLACKDUCK_CA_BUNDLE=/path/ca.pem   trust an internal CA — verification STAYS ON
  BLACKDUCK_INSECURE_TLS=0           force TLS verify on

Black Duck does not take the API token on a data call. The token is exchanged
for a short-lived bearer first (``POST /api/tokens/authenticate``); the bearer
is kept in memory until shortly before it expires.

A Black Duck project is not a git repository. The tie between the two is the
project name and version name the pipeline hands to Detect, so the reliable
address is the link from that pipeline's own log.

Soft-error contract: every function returns ``{"ok": bool, ...}`` and never
raises into the agent loop.
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
import urllib.parse

from . import _http_integration as _http

_TIMEOUT_S = 30
_DEFAULT_ROWS = 20
_MAX_ROWS = 100
_PAGE = 100
# Pages walked for components and for vulnerabilities. A BOM of a large
# service runs to several hundred components.
_MAX_PAGES = 5
# Violating components whose broken rules are looked up — one call each.
_MAX_RULE_LOOKUPS = 5
_ID = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
_LINK = re.compile(rf"/projects/({_ID})(?:/versions/({_ID}))?")
_SEVERITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
# Remediation statuses that mean somebody has already dealt with it.
_HANDLED = frozenset({"IGNORED", "PATCHED", "MITIGATED", "DUPLICATE",
                      "REMEDIATION_COMPLETE", "NOT_AFFECTED"})
_NOT_CONFIGURED = {"ok": False, "error": "blackduck_not_configured",
                   "hint": "set BLACKDUCK_BASE_URL + BLACKDUCK_TOKEN, or fill "
                           "in Settings → Integrations → Black Duck"}

_bearers: dict[str, tuple[str, float]] = {}
_bearer_lock = threading.Lock()


def _conf() -> dict:
    return _http.integration_conf("blackduck", "BLACKDUCK")


def _base() -> str:
    return _conf()["base_url"]


def _configured() -> bool:
    c = _conf()
    return bool(c["base_url"] and c["token"])


def _ssl_ctx():
    c = _conf()
    return _http.ssl_context(c["insecure_tls"], c.get("ca_bundle", ""),
                             c.get("base_url", ""))


def _ssl_ctx_or_error():
    """``(context, error)`` — an unreadable CA bundle is an answer, not a
    raise."""
    try:
        return _ssl_ctx(), None
    except ValueError as exc:
        return None, {"ok": False, "error": str(exc)}


def _cache_key() -> str:
    c = _conf()
    digest = hashlib.sha256(c["token"].encode("utf-8")).hexdigest()
    return c["base_url"] + "|" + digest


def _bearer(*, fresh: bool = False) -> tuple[str, dict | None]:
    """``(bearer, error)``. One exchange serves every call until the bearer is
    a minute from expiring."""
    key = _cache_key()
    context, ctx_err = _ssl_ctx_or_error()
    if ctx_err:
        return "", ctx_err
    with _bearer_lock:
        held = _bearers.get(key)
        if held and not fresh and held[1] > time.monotonic():
            return held[0], None
        r = _http.http_request(
            "POST", _base() + "/api/tokens/authenticate",
            headers={"Authorization": "token " + _conf()["token"],
                     "Accept": "application/vnd.blackducksoftware.user-4+json",
                     "User-Agent": "AIForgeCrew-BlackDuck/1.0"},
            timeout=_TIMEOUT_S, context=context, credential_exchange=True)
        if not r["ok"]:
            _bearers.pop(key, None)
            return "", _explain(r)
        data = r["data"] if isinstance(r["data"], dict) else {}
        bearer = str(data.get("bearerToken") or "")
        if not bearer:
            return "", {"ok": False, "error": "unexpected_payload",
                        "hint": "Black Duck answered the token exchange "
                                "without a bearer token"}
        try:
            life_s = int(data.get("expiresInMilliseconds") or 0) / 1000
        except (TypeError, ValueError):
            life_s = 0
        _bearers[key] = (bearer, time.monotonic() + max(0.0, life_s - 60))
        return bearer, None


def _explain(r: dict) -> dict:
    err = str(r.get("error") or "")
    out = dict(r)
    if err.startswith("http 401"):
        out["hint"] = ("Black Duck rejected the API token — it may be revoked "
                       "or expired")
    elif err.startswith("http 403"):
        out["hint"] = "the token's user has no access to this project"
    elif err.startswith("http 404"):
        out["hint"] = "Black Duck has no such project or version"
    return out


def _request(path: str, params: dict | None = None) -> dict:
    """GET ``path`` under the configured server, with the bearer."""
    if not _configured():
        return dict(_NOT_CONFIGURED)
    url = _base() + path
    if params:
        url += "?" + urllib.parse.urlencode(
            {k: v for k, v in params.items() if v not in (None, "")})
    r: dict = {}
    for attempt in (0, 1):
        bearer, err = _bearer(fresh=attempt == 1)
        if err:
            return err
        context, ctx_err = _ssl_ctx_or_error()
        if ctx_err:
            return ctx_err
        r = _http.http_request(
            "GET", url, timeout=_TIMEOUT_S, context=context,
            body_cap=2_000_000,
            headers={"Authorization": "Bearer " + bearer,
                     "Accept": "application/json",
                     "User-Agent": "AIForgeCrew-BlackDuck/1.0"})
        # A bearer the server no longer honours (restart, revoked session):
        # exchange once more before reporting the token as bad.
        if r["ok"] or not str(r.get("error", "")).startswith("http 401"):
            return r if r["ok"] else _explain(r)
    return _explain(r)


# ─────────────────────────── addressing ───────────────────────────

def on_our_server(url: str) -> bool:
    """Is ``url`` on the configured Black Duck server? A link anywhere else is
    refused, not followed — the token goes only where the operator pointed."""
    return _http.same_origin(url, _base())


def parse_link(url: str) -> dict:
    """``{"project_id", "version_id"}`` from a Black Duck link (API or UI
    form). ``version_id`` is "" for a project-only link; ``{}`` for no match."""
    try:
        path = urllib.parse.urlsplit(str(url or "").strip()).path
    except ValueError:
        return {}
    m = _LINK.search(path)
    if not m:
        return {}
    return {"project_id": m.group(1), "version_id": m.group(2) or ""}


def _items(r: dict) -> list:
    data = r.get("data") if isinstance(r.get("data"), dict) else {}
    return [x for x in data.get("items") or [] if isinstance(x, dict)]


def _href_id(item: dict, after: str) -> str:
    """The id that follows ``/<after>/`` in an item's own link."""
    meta = item.get("_meta") if isinstance(item.get("_meta"), dict) else {}
    m = re.search(rf"/{after}/({_ID})", str(meta.get("href") or ""))
    return m.group(1) if m else ""


def _project_by_name(name: str) -> tuple[str, dict | None]:
    # `q=name:` is a "contains" search: the exact name can sit behind many
    # longer ones, so ask for a full page of them.
    r = _request("/api/projects", {"q": "name:" + name, "limit": _PAGE})
    if not r["ok"]:
        return "", r
    rows = _items(r)
    for row in rows:
        if str(row.get("name") or "").lower() == name.lower():
            return _href_id(row, "projects"), None
    return "", {"ok": False, "error": "project_not_found",
                "hint": f"no Black Duck project is named {name!r}",
                "similar": [row.get("name") for row in rows][:10]}


def _version_by_name(project_id: str, name: str) -> tuple[str, dict | None]:
    params = {"limit": 25}
    if name:
        params["q"] = "versionName:" + name
    r = _request(f"/api/projects/{project_id}/versions", params)
    if not r["ok"]:
        return "", r
    rows = _items(r)
    for row in rows:
        if name and str(row.get("versionName") or "").lower() == name.lower():
            return _href_id(row, "versions"), None
    if not name and len(rows) == 1:
        return _href_id(rows[0], "versions"), None
    names = [row.get("versionName") for row in rows][:15]
    if name:
        return "", {"ok": False, "error": "version_not_found",
                    "hint": f"this project has no version named {name!r}",
                    "versions": names}
    return "", {"ok": False, "error": "missing 'version'",
                "hint": "this project has several versions — pass one "
                        "(a pipeline usually names it after the branch)",
                "versions": names}


def _target(args: dict) -> tuple[dict, dict | None]:
    """``({"project_id", "version_id"}, error)``."""
    url = str(args.get("url") or "").strip()
    project = str(args.get("project") or "").strip()
    version = str(args.get("version") or "").strip()
    ids = {"project_id": "", "version_id": ""}
    if url:
        if not on_our_server(url):
            return {}, {"ok": False,
                        "error": "link_not_on_configured_blackduck",
                        "hint": f"the configured Black Duck is {_base()}; a "
                                "link on another host is not opened"}
        ids = parse_link(url)
        if not ids:
            return {}, {"ok": False, "error": "unrecognised_blackduck_link",
                        "hint": "pass the project-version link Detect prints "
                                "(…/api/projects/<id>/versions/<id>/"
                                "components), or project= and version="}
    elif not project:
        return {}, {"ok": False, "error": "missing 'project'",
                    "hint": "pass url=<Black Duck link>, or project=<name> "
                            "and version=<name>"}
    if not ids["project_id"]:
        ids["project_id"], err = _project_by_name(project)
        if err:
            return {}, err
    if not ids["version_id"]:
        ids["version_id"], err = _version_by_name(ids["project_id"], version)
        if err:
            return {}, err
    if not ids["project_id"] or not ids["version_id"]:
        return {}, {"ok": False, "error": "unexpected_payload",
                    "hint": "Black Duck listed the project without a link "
                            "to it"}
    return ids, None


# ─────────────────────────── sections ───────────────────────────

def _counts(rows) -> dict:
    return {str(c.get("name")): c.get("value") for c in rows or []
            if isinstance(c, dict) and c.get("value")}


def _policy(version_path: str) -> dict:
    r = _request(version_path + "/policy-status")
    if not r["ok"]:
        return {"error": r.get("error")}
    data = r["data"] if isinstance(r["data"], dict) else {}
    status = str(data.get("overallStatus") or "").upper()
    return {"status": status, "in_violation": status == "IN_VIOLATION",
            "components": _counts(data.get("componentVersionStatusCounts"))}


def _pages(path: str) -> tuple[list, int | None, str]:
    """``(rows, total, note)`` for a paged list, up to ``_MAX_PAGES``."""
    rows: list = []
    total = None
    for page in range(_MAX_PAGES):
        r = _request(path, {"limit": _PAGE, "offset": page * _PAGE})
        if not r["ok"]:
            if not rows:
                return [], None, f"could not be read ({r.get('error')})"
            return rows, total, (f"listing stopped after {len(rows)} rows "
                                 f"({r.get('error')})")
        if r.get("body_cap_hit") or not isinstance(r.get("data"), dict):
            # Too large to parse, or not JSON: unread, which is not "empty".
            return rows, total, (f"listing stopped after {len(rows)} rows "
                                 "(a page could not be parsed)")
        batch = _items(r)
        rows.extend(batch)
        data = r["data"]
        if isinstance(data.get("totalCount"), int):
            total = data["totalCount"]
        if len(batch) < _PAGE:
            return rows, total, ""
    if total is not None and total <= len(rows):
        return rows, total, ""
    return rows, total, (f"only the first {len(rows)} rows were read — the "
                         "list is longer")


def _rule_names(component: dict) -> list:
    """The policy rules one component breaks. Its own link is followed only as
    a path under the configured server."""
    meta = component.get("_meta") if isinstance(component.get("_meta"), dict) else {}
    try:
        path = urllib.parse.urlsplit(str(meta.get("href") or "")).path
    except ValueError:
        return []
    at = path.find("/api/projects/")
    if at < 0:
        return []
    r = _request(path[at:] + "/policy-rules", {"limit": 25})
    if not r["ok"]:
        return []
    return [{"rule": row.get("name"), "severity": row.get("severity")}
            for row in _items(r)]


def _licenses(component: dict) -> str:
    rows = component.get("licenses")
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        return str(rows[0].get("licenseDisplay") or "")
    return ""


def _violations(version_path: str, rows_wanted: int) -> dict:
    rows, _total, note = _pages(version_path + "/components")
    bad = [c for c in rows
           if str(c.get("policyStatus") or "").upper() == "IN_VIOLATION"]
    shown = []
    for n, comp in enumerate(bad[:rows_wanted]):
        entry = {"component": comp.get("componentName"),
                 "version": comp.get("componentVersionName"),
                 "license": _licenses(comp)}
        if n < _MAX_RULE_LOOKUPS:
            entry["rules"] = _rule_names(comp)
        shown.append(entry)
    out = {"count": len(bad), "shown": shown}
    if note:
        out["note"] = "components: " + note
    return out


def _vuln_row(item: dict) -> dict:
    v = item.get("vulnerabilityWithRemediation")
    v = v if isinstance(v, dict) else {}
    return {"id": v.get("vulnerabilityName"),
            "severity": str(v.get("severity") or "").upper(),
            "score": v.get("overallScore", v.get("baseScore")),
            "status": str(v.get("remediationStatus") or "").upper(),
            "component": item.get("componentName"),
            "version": item.get("componentVersionName")}


def _score(row: dict) -> float:
    try:
        return float(row.get("score") or 0)
    except (TypeError, ValueError):
        return 0.0


def _vulnerabilities(version_path: str, rows_wanted: int) -> dict:
    rows, _total, note = _pages(version_path + "/vulnerable-bom-components")
    found = [_vuln_row(r) for r in rows]
    is_open = [v for v in found if v["status"] not in _HANDLED]
    is_open.sort(key=lambda v: (_SEVERITY_ORDER.get(v["severity"], 9),
                                -_score(v)))
    by_severity: dict = {}
    for v in is_open:
        by_severity[v["severity"]] = by_severity.get(v["severity"], 0) + 1
    out = {"open": len(is_open), "by_severity": by_severity,
           "already_handled": len(found) - len(is_open),
           "shown": is_open[:rows_wanted]}
    if note:
        out["note"] = "vulnerabilities: " + note
    elif len(is_open) > rows_wanted:
        out["note"] = (f"showing the {rows_wanted} most severe of "
                       f"{len(is_open)} open vulnerabilities")
    return out


def _rows(args: dict) -> int:
    try:
        n = int(args.get("limit") or _DEFAULT_ROWS)
    except (TypeError, ValueError, OverflowError):
        n = _DEFAULT_ROWS
    return max(1, min(_MAX_ROWS, n))


# ─────────────────────────── tools ───────────────────────────

def blackduck_report(args: dict, _cwd: str | None = None) -> dict:
    """READ one project version's scan result: policy status, the components
    in violation with the rules they break, and the open vulnerabilities.

    Address it with ``url`` (the Black Duck link Detect prints in a CI job
    log) or with ``project`` and ``version`` names. ``limit`` caps the
    components and vulnerabilities listed."""
    args = args or {}
    if not _configured():
        return dict(_NOT_CONFIGURED)
    ids, err = _target(args)
    if err:
        return err
    version_path = (f"/api/projects/{ids['project_id']}"
                    f"/versions/{ids['version_id']}")
    head = _request(version_path)
    if not head["ok"]:
        return head
    project = _request(f"/api/projects/{ids['project_id']}")
    rows = _rows(args)
    out = {"ok": True,
           "project": (project.get("data") or {}).get("name")
           if project.get("ok") and isinstance(project.get("data"), dict)
           else str(args.get("project") or ""),
           "version": (head["data"] or {}).get("versionName")
           if isinstance(head["data"], dict) else "",
           "url": _base() + version_path + "/components"}
    out["policy"] = _policy(version_path)
    if out["policy"].get("in_violation"):
        out["policy_violations"] = _violations(version_path, rows)
    out["vulnerabilities"] = _vulnerabilities(version_path, rows)
    return out


def blackduck_test() -> dict:
    """Connectivity + token check for the Settings UI."""
    if not _configured():
        return {"ok": False, "error": "blackduck_not_configured"}
    _bearer_err = _bearer(fresh=True)[1]
    if _bearer_err:
        return {**_bearer_err, "base_url": _base()}
    r = _request("/api/current-user")
    if not r["ok"]:
        return {**r, "base_url": _base()}
    data = r["data"] if isinstance(r["data"], dict) else {}
    return {"ok": True, "base_url": _base(), "user": data.get("userName")}


__all__ = ["blackduck_report", "blackduck_test", "parse_link",
           "on_our_server"]
