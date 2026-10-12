"""SonarQube tool — read what a scan found: quality gate, issues, hotspots.

Read-only. The agent gets a Sonar link (from a CI job log, or from the user)
or a project key, and this answers with the same facts the dashboard shows,
through the Web API and a token. The dashboard itself is a login page to
anything that does not hold a browser session.

Config (env, or Settings → Integrations):
  SONAR_BASE_URL   e.g. https://sonar.internal  (no trailing /)
  SONAR_TOKEN      a user token. It needs "Browse" on the project, and
                   "See Source Code" for issue locations. Opening the
                   scanner's own task link (…/api/ce/task?id=…) also needs
                   "Execute Analysis"; without it the result is read by
                   project key instead.
  SONAR_CA_BUNDLE=/path/ca.pem   trust an internal CA — verification STAYS ON
  SONAR_INSECURE_TLS=0           force TLS verify on

The token is sent as HTTP Basic with an empty password. Every SonarQube
version accepts that; ``Authorization: Bearer`` only exists from 10.0.

Soft-error contract: every function returns ``{"ok": bool, ...}`` and never
raises into the agent loop.
"""
from __future__ import annotations

import base64
import urllib.parse

from . import _http_integration as _http

_TIMEOUT_S = 20
_DEFAULT_ROWS = 20
_MAX_ROWS = 100
_METRICS = ("new_coverage", "new_duplicated_lines_density", "coverage",
            "duplicated_lines_density", "bugs", "vulnerabilities",
            "code_smells", "security_hotspots")
_NOT_CONFIGURED = {"ok": False, "error": "sonar_not_configured",
                   "hint": "set SONAR_BASE_URL + SONAR_TOKEN, or fill in "
                           "Settings → Integrations → Sonar"}


def _conf() -> dict:
    return _http.integration_conf("sonar", "SONAR")


def _base() -> str:
    return _conf()["base_url"]


def _configured() -> bool:
    c = _conf()
    return bool(c["base_url"] and c["token"])


def _headers() -> dict[str, str]:
    raw = (_conf()["token"] + ":").encode("utf-8")
    return {"Accept": "application/json",
            "Authorization": "Basic " + base64.b64encode(raw).decode("ascii"),
            "User-Agent": "AIForgeCrew-Sonar/1.0"}


def _ssl_ctx():
    c = _conf()
    return _http.ssl_context(c["insecure_tls"], c.get("ca_bundle", ""),
                             c.get("base_url", ""))


def _request(path: str, params: dict | None = None, *,
             parse_json: bool = True) -> dict:
    if not _configured():
        return dict(_NOT_CONFIGURED)
    url = _base() + path
    if params:
        url += "?" + urllib.parse.urlencode(
            {k: v for k, v in params.items() if v not in (None, "")})
    try:
        context = _ssl_ctx()
    except ValueError as exc:       # an unreadable CA bundle
        return {"ok": False, "error": str(exc)}
    return _http.http_request("GET", url, headers=_headers(),
                              timeout=_TIMEOUT_S, context=context,
                              parse_json=parse_json)


# ─────────────────────────── addressing ───────────────────────────

def on_our_server(url: str) -> bool:
    """Is ``url`` on the configured Sonar server?

    The link comes from a job log or from the model. The token goes only to
    the host the operator configured, so a link anywhere else is refused, not
    followed."""
    return _http.same_origin(url, _base())


def parse_link(url: str) -> dict:
    """What a Sonar link points at: ``{"task_id"}`` for the background-task
    link a scanner prints, else ``{"project", "branch", "pull_request"}`` for
    a dashboard-style link. ``{}`` when it names neither."""
    try:
        parts = urllib.parse.urlsplit(str(url or "").strip())
        query = urllib.parse.parse_qs(parts.query)
    except ValueError:
        return {}

    def first(*names: str) -> str:
        for name in names:
            vals = query.get(name) or []
            if vals and vals[0].strip():
                return vals[0].strip()
        return ""

    if parts.path.rstrip("/").endswith("/api/ce/task"):
        return {"task_id": first("id")} if first("id") else {}
    key = first("id", "projectKey", "project", "component")
    if not key:
        return {}
    return {"project": key, "branch": first("branch"),
            "pull_request": first("pullRequest")}


def _target(args: dict) -> tuple[dict, dict | None]:
    """``(target, error)``. ``target`` has ``project`` and may have ``branch``,
    ``pull_request``, ``analysis_id`` and ``task``."""
    url = str(args.get("url") or "").strip()
    target = {"project": str(args.get("project") or "").strip(),
              "branch": str(args.get("branch") or "").strip(),
              "pull_request": str(args.get("pull_request") or "").strip()}
    if url:
        if not on_our_server(url):
            return {}, {"ok": False, "error": "link_not_on_configured_sonar",
                        "hint": f"the configured Sonar is {_base()}; a link "
                                "on another host is not opened"}
        found = parse_link(url)
        if not found:
            return {}, {"ok": False, "error": "unrecognised_sonar_link",
                        "hint": "pass a dashboard link (…?id=<projectKey>), a "
                                "scanner task link (…/api/ce/task?id=…), or "
                                "project=<projectKey>"}
        if found.get("task_id"):
            return _target_from_task(found["task_id"])
        target.update({k: v for k, v in found.items() if v})
    if not target["project"]:
        return {}, {"ok": False, "error": "missing 'project'",
                    "hint": "pass project=<projectKey> or url=<Sonar link>"}
    return target, None


def _target_from_task(task_id: str) -> tuple[dict, dict | None]:
    """The scanner prints a background-task link, not a project link. The task
    names the project, the branch or pull request, and the analysis."""
    r = _request("/api/ce/task", {"id": task_id})
    if not r["ok"]:
        out = _explain(r)
        if str(r.get("error") or "").startswith("http 403"):
            out["hint"] = ("this token may not read the scanner's task link "
                           "(it needs 'Execute Analysis' on the project) — "
                           "pass project=<projectKey> with the branch or "
                           "pull_request instead")
        return {}, out
    task = (r["data"] or {}).get("task") if isinstance(r["data"], dict) else None
    if not isinstance(task, dict) or not task.get("componentKey"):
        return {}, {"ok": False, "error": "unexpected_payload",
                    "hint": "the task link did not name a project"}
    return {"project": str(task["componentKey"]),
            "branch": str(task.get("branch") or ""),
            "pull_request": str(task.get("pullRequest") or ""),
            "analysis_id": str(task.get("analysisId") or ""),
            "task": {"status": task.get("status"),
                     "error": task.get("errorMessage")}}, None


def _explain(r: dict) -> dict:
    """Add the likely cause to an auth or not-found answer."""
    err = str(r.get("error") or "")
    out = dict(r)
    if err.startswith("http 401"):
        out["hint"] = "Sonar rejected the token — it may be revoked or expired"
    elif err.startswith("http 403"):
        out["hint"] = ("the token's user has no 'Browse' permission on this "
                       "project")
    elif err.startswith("http 404"):
        out["hint"] = ("Sonar has no such project, branch or pull request — "
                       "check the project key")
    return out


def _scope(target: dict) -> dict:
    """The branch / pull request selector every project endpoint takes."""
    if target.get("pull_request"):
        return {"pullRequest": target["pull_request"]}
    if target.get("branch"):
        return {"branch": target["branch"]}
    return {}


# ─────────────────────────── sections ───────────────────────────

def _quality_gate(target: dict) -> dict:
    if target.get("analysis_id"):
        params = {"analysisId": target["analysis_id"]}
    else:
        params = {"projectKey": target["project"], **_scope(target)}
    r = _request("/api/qualitygates/project_status", params)
    if not r["ok"]:
        return _explain(r)
    status = (r["data"] or {}).get("projectStatus") if isinstance(r["data"], dict) else None
    if not isinstance(status, dict):
        return {"ok": False, "error": "unexpected_payload"}
    conditions = [c for c in status.get("conditions") or [] if isinstance(c, dict)]
    failed = [{"metric": c.get("metricKey"), "actual": c.get("actualValue"),
               "comparator": c.get("comparator"),
               "threshold": c.get("errorThreshold")}
              for c in conditions if str(c.get("status")).upper() == "ERROR"]
    gate = str(status.get("status") or "").upper()
    return {"ok": True, "status": gate, "passed": gate == "OK",
            "failed_conditions": failed}


def _either(path: str, params: dict, name: str, newer: str) -> dict:
    """GET with a parameter Sonar renamed between versions.

    The older name goes first. Sonar IGNORES a parameter it does not know, so
    sending the newer name to an older server would drop the project filter
    and answer for every project the token can see."""
    r = _request(path, params)
    if r["ok"] or not str(r.get("error", "")).startswith("http 400"):
        return r
    renamed = {newer if k == name else k: v for k, v in params.items()}
    return _request(path, renamed)


def _rows(args: dict) -> int:
    try:
        n = int(args.get("limit") or _DEFAULT_ROWS)
    except (TypeError, ValueError, OverflowError):
        n = _DEFAULT_ROWS
    return max(1, min(_MAX_ROWS, n))


def _file_of(component, project: str) -> str:
    """``projectKey:src/a.py`` → ``src/a.py``."""
    text = str(component or "")
    return text[len(project) + 1:] if text.startswith(project + ":") else text


def _facet(data: dict, name: str) -> dict:
    for facet in data.get("facets") or []:
        if isinstance(facet, dict) and facet.get("property") == name:
            return {v.get("val"): v.get("count") for v in facet.get("values") or []
                    if isinstance(v, dict) and v.get("count")}
    return {}


def _issues(target: dict, rows: int) -> dict:
    r = _either("/api/issues/search",
                {"componentKeys": target["project"], "resolved": "false",
                 "ps": rows, "s": "SEVERITY", "asc": "false",
                 "facets": "severities,types", **_scope(target)},
                "componentKeys", "components")
    if not r["ok"]:
        return {"error": r.get("error")}
    data = r["data"] if isinstance(r["data"], dict) else {}
    paging = data.get("paging") if isinstance(data.get("paging"), dict) else {}
    found = [i for i in data.get("issues") or [] if isinstance(i, dict)]
    total = paging.get("total", data.get("total", len(found)))
    out = {"total": total,
           "by_severity": _facet(data, "severities"),
           "by_type": _facet(data, "types"),
           "shown": [{"severity": i.get("severity"), "type": i.get("type"),
                      "rule": i.get("rule"),
                      "file": _file_of(i.get("component"), target["project"]),
                      "line": i.get("line"),
                      "message": str(i.get("message") or "")[:300]}
                     for i in found]}
    if isinstance(total, int) and total > len(found):
        out["note"] = (f"showing the {len(found)} most severe of {total} "
                       "open issues")
    return out


def _hotspots(target: dict, rows: int) -> dict:
    r = _either("/api/hotspots/search",
                {"projectKey": target["project"], "status": "TO_REVIEW",
                 "ps": rows, **_scope(target)}, "projectKey", "project")
    if not r["ok"]:
        return {"error": r.get("error")}
    data = r["data"] if isinstance(r["data"], dict) else {}
    paging = data.get("paging") if isinstance(data.get("paging"), dict) else {}
    found = [h for h in data.get("hotspots") or [] if isinstance(h, dict)]
    return {"to_review": paging.get("total", len(found)),
            "shown": [{"risk": h.get("vulnerabilityProbability"),
                       "category": h.get("securityCategory"),
                       "file": _file_of(h.get("component"), target["project"]),
                       "line": h.get("line"),
                       "message": str(h.get("message") or "")[:300]}
                      for h in found]}


def _measure_value(m: dict):
    """A ``new_*`` metric carries its value under ``period`` (``periods`` on
    old servers), the rest under ``value``."""
    if "value" in m:
        return m["value"]
    period = m.get("period")
    if isinstance(period, dict):
        return period.get("value")
    periods = m.get("periods")
    if isinstance(periods, list) and periods and isinstance(periods[0], dict):
        return periods[0].get("value")
    return None


def _measures(target: dict) -> dict:
    r = _request("/api/measures/component",
                 {"component": target["project"],
                  "metricKeys": ",".join(_METRICS), **_scope(target)})
    if not r["ok"]:
        return {"error": r.get("error")}
    comp = (r["data"] or {}).get("component") if isinstance(r["data"], dict) else None
    rows = comp.get("measures") if isinstance(comp, dict) else None
    out = {}
    for m in rows or []:
        if isinstance(m, dict) and _measure_value(m) is not None:
            out[str(m.get("metric"))] = _measure_value(m)
    return out


def _dashboard(target: dict) -> str:
    query = {"id": target["project"], **_scope(target)}
    return _base() + "/dashboard?" + urllib.parse.urlencode(query)


# ─────────────────────────── tools ───────────────────────────

def sonar_report(args: dict, _cwd: str | None = None) -> dict:
    """READ one project's scan result: quality gate, open issues, security
    hotspots and the headline measures.

    Address it with ``url`` (a Sonar link, as printed in a CI job log) or
    ``project`` (the project key), with an optional ``branch`` or
    ``pull_request``. ``limit`` caps the issues and hotspots listed."""
    args = args or {}
    if not _configured():
        return dict(_NOT_CONFIGURED)
    target, err = _target(args)
    if err:
        return err
    out = {"ok": True, "project": target["project"]}
    for key in ("branch", "pull_request"):
        if target.get(key):
            out[key] = target[key]
    out["url"] = _dashboard(target)
    task = target.get("task") or {}
    status = str(task.get("status") or "").upper()
    if status and status != "SUCCESS":
        # Sonar has not finished (or has failed) turning the scanner's upload
        # into a result. A gate read now would be the PREVIOUS analysis.
        out["analysis"] = {"status": status, "error": task.get("error")}
        out["hint"] = ("Sonar is still processing this analysis — read it "
                       "again shortly" if status in ("PENDING", "IN_PROGRESS")
                       else "Sonar could not process this analysis")
        return out
    gate = _quality_gate(target)
    if not gate["ok"]:
        return gate
    rows = _rows(args)
    out["quality_gate"] = {k: v for k, v in gate.items() if k != "ok"}
    out["issues"] = _issues(target, rows)
    out["hotspots"] = _hotspots(target, rows)
    out["measures"] = _measures(target)
    return out


def sonar_test() -> dict:
    """Connectivity + token check for the Settings UI."""
    if not _configured():
        return {"ok": False, "error": "sonar_not_configured"}
    r = _request("/api/authentication/validate")
    if not r["ok"]:
        return {**_explain(r), "base_url": _base()}
    if not (isinstance(r["data"], dict) and r["data"].get("valid")):
        return {"ok": False, "error": "token rejected", "base_url": _base(),
                "hint": "Sonar answered, but does not accept this token"}
    version = _request("/api/server/version", parse_json=False)
    return {"ok": True, "base_url": _base(),
            "version": str(version.get("data") or "").strip()[:40]
            if version.get("ok") else ""}


__all__ = ["sonar_report", "sonar_test", "parse_link", "on_our_server"]
