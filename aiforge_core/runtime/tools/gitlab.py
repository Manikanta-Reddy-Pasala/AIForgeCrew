"""GitLab (self-managed / SaaS) tool — search / read / create / update issues.

Lets the chat agent pull a GitLab issue in, analyse it, file a new one, edit
fields, or drop a comment (note). REST API v4 (``/api/v4``).

Config (env):
  GITLAB_BASE_URL   e.g. https://gitlab.internal  (no trailing /, no /api/v4)
  GITLAB_TOKEN      Personal / Project / Group Access Token (sent as
                    ``PRIVATE-TOKEN``). An OAuth token works too (set
                    GITLAB_OAUTH=1 → sent as ``Authorization: Bearer``).
  GITLAB_PROJECT    (optional) default project — numeric id or URL path
                    ("group/sub/project"). Used when a call omits ``project``.
  GITLAB_INSECURE_TLS=1   skip TLS verify for a self-signed internal cert

GitLab issues are addressed per-project by their ``iid`` (the number you see
in the UI, e.g. #42), NOT the global id — every read/update/comment needs a
project + iid.

Soft-error contract: every function returns ``{"ok": bool, ...}`` and never
raises into the agent loop.
"""
from __future__ import annotations

import os
import urllib.parse

from . import _http_integration as _http

_PASS_PROJECT_GROUP_PROJ_OR_S = 'pass project=\\"group/proj\\" or set GITLAB_PROJECT'
_MISSING_IID = "missing 'iid'"
_MISSING_PROJECT = "missing 'project'"

_TIMEOUT_S = 20
_BODY_CAP = 200_000


def _conf() -> dict:
    """Resolve config via the shared integration helper: base_url/token/
    insecure_tls (insecure by default) + project + oauth."""
    return _http.integration_conf(
        "gitlab", "GITLAB",
        str_fields=(("project", "GITLAB_PROJECT"),),
        bool_fields=(("oauth", "GITLAB_OAUTH"),))


def _auth_scheme() -> str:
    return "bearer" if _conf()["oauth"] else "private-token"


def _base() -> str:
    return _conf()["base_url"]


def _api() -> str:
    return _base() + "/api/v4"


def _configured() -> bool:
    c = _conf()
    return bool(c["base_url"] and c["token"])


def _headers() -> dict[str, str]:
    c = _conf()
    h = {"Content-Type": "application/json", "Accept": "application/json",
         "User-Agent": "AIForgeCrew-GitLab/1.0"}
    # GitLab accepts a PAT as PRIVATE-TOKEN; an OAuth token as Bearer.
    if c["oauth"]:
        h["Authorization"] = "Bearer " + c["token"]
    else:
        h["PRIVATE-TOKEN"] = c["token"]
    return h


def _ssl_ctx():
    return _http.ssl_context(_conf()["insecure_tls"])


def _proj_id(args: dict | None = None) -> str:
    """Project from the call args, else the configured default."""
    p = ((args or {}).get("project") or (args or {}).get("repo")
         or _conf()["project"] or "")
    return str(p).strip()


def _enc_proj(project: str) -> str:
    """URL-encode a project path ("group/project" → "group%2Fproject").
    A numeric id passes through unchanged."""
    return urllib.parse.quote(str(project), safe="")


def _request(method: str, path: str, *, params: dict | None = None,
             body: dict | None = None, body_cap: int = _BODY_CAP,
             capture_headers: tuple = (), parse_json: bool = True) -> dict:
    if not _configured():
        return {"ok": False, "error": "gitlab_not_configured",
                "hint": "set GITLAB_BASE_URL + GITLAB_TOKEN"}
    url = _api() + path
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    return _http.http_request(method, url, headers=_headers(), body=body,
                              timeout=_TIMEOUT_S, body_cap=body_cap,
                              context=_ssl_ctx(),
                              capture_headers=capture_headers,
                              parse_json=parse_json)


def _issue_summary(d: dict) -> dict:
    """Compact one-line view of an issue search hit."""
    if not isinstance(d, dict):
        return {}
    return {
        "iid": d.get("iid"),
        "project_id": d.get("project_id"),
        "title": d.get("title"),
        "state": d.get("state"),
        "labels": d.get("labels") or [],
        "assignee": ((d.get("assignee") or {}) or {}).get("name"),
        "url": d.get("web_url"),
    }


# ─────────────────────────── tools ──────────────────────────────────

def gitlab_search(args: dict, _cwd: str | None = None) -> dict:
    """Find issues. ``query`` (or ``search``) full-text. Optional ``project``
    (defaults to GITLAB_PROJECT — omit to search ALL accessible issues),
    ``state`` (opened|closed|all, default all), ``labels`` (comma/list),
    ``limit``."""
    q = (args.get("query") or args.get("search") or "").strip()
    params: dict = {"per_page": int(args.get("limit", 20))}
    if q:
        params["search"] = q
    state = (args.get("state") or "all").strip().lower()
    if state in ("opened", "closed"):
        params["state"] = state
    labels = args.get("labels")
    if labels:
        if isinstance(labels, (list, tuple)):
            labels = ",".join(str(x) for x in labels)
        params["labels"] = labels
    proj = _proj_id(args)
    path = f"/projects/{_enc_proj(proj)}/issues" if proj else "/issues"
    if not proj:
        params["scope"] = "all"
    r = _request("GET", path, params=params)
    if not r["ok"]:
        return r
    rows = r["data"] if isinstance(r["data"], list) else []
    return {"ok": True, "results": [_issue_summary(x) for x in rows]}


def _issue_notes(proj: str, enc: str) -> list:
    """The human comments on an issue (system notes filtered out)."""
    n = _request("GET", f"/projects/{_enc_proj(proj)}/issues/{enc}/notes",
                 params={"per_page": 50, "sort": "asc"})
    if not (n["ok"] and isinstance(n["data"], list)):
        return []
    return [{"author": ((c.get("author") or {}) or {}).get("name"),
             "body": (c.get("body") or "")[:4000]}
            for c in n["data"] if not c.get("system")]


def _addressed(args: dict) -> tuple[str, str, dict | None]:
    """``(project, encoded iid, error)`` for an issue-addressed call."""
    proj = _proj_id(args)
    iid = str(args.get("iid") or args.get("id") or args.get("key") or "").strip()
    if not proj:
        return "", "", {"ok": False, "error": _MISSING_PROJECT}
    if not iid:
        return "", "", {"ok": False, "error": _MISSING_IID}
    enc, ierr = _iid_or_error(iid)
    return proj, enc, ierr


def gitlab_read(args: dict, _cwd: str | None = None) -> dict:
    """Read an issue by ``project`` + ``iid`` (the #number). Returns fields +
    comments. ``project`` defaults to GITLAB_PROJECT."""
    proj, enc, err = _addressed(args)
    if err:
        return err
    r = _request("GET", f"/projects/{_enc_proj(proj)}/issues/{enc}")
    if not r["ok"]:
        return r
    d = r["data"] if isinstance(r["data"], dict) else {}
    return {"ok": True, "iid": d.get("iid"), "title": d.get("title"),
            "state": d.get("state"),
            "author": ((d.get("author") or {}) or {}).get("name"),
            "assignee": ((d.get("assignee") or {}) or {}).get("name"),
            "labels": d.get("labels") or [],
            "description": (d.get("description") or "")[:_BODY_CAP],
            "comments": _issue_notes(proj, enc), "url": d.get("web_url")}


def gitlab_create(args: dict, _cwd: str | None = None) -> dict:
    """Create an issue. Required: ``project`` (or GITLAB_PROJECT), ``title``.
    Optional: ``description``, ``labels`` (list/csv), ``assignee_ids`` (list)."""
    proj = _proj_id(args)
    if not proj:
        return {"ok": False, "error": _MISSING_PROJECT}
    if not args.get("title"):
        return {"ok": False, "error": "missing 'title'"}
    body: dict = {"title": args["title"]}
    if args.get("description"):
        body["description"] = args["description"]
    if args.get("labels"):
        labels = args["labels"]
        if isinstance(labels, (list, tuple)):
            labels = ",".join(str(x) for x in labels)
        body["labels"] = labels
    if args.get("assignee_ids"):
        ids = args["assignee_ids"]
        if isinstance(ids, str):
            ids = [int(s) for s in ids.replace(",", " ").split() if s.strip().isdigit()]
        body["assignee_ids"] = list(ids)
    r = _request("POST", f"/projects/{_enc_proj(proj)}/issues", body=body)
    if not r["ok"]:
        return r
    d = r["data"] if isinstance(r["data"], dict) else {}
    return {"ok": True, "iid": d.get("iid"), "url": d.get("web_url"),
            "written": {"title": args.get("title"), "description": args.get("description")}}


def gitlab_update(args: dict, _cwd: str | None = None) -> dict:
    """Update an issue. Required: ``project`` + ``iid``. Provide any of
    ``title``, ``description``, ``labels`` (list/csv), ``state_event``
    (close|reopen), or a raw ``fields`` dict (merged last, wins)."""
    proj = _proj_id(args)
    iid = str(args.get("iid") or args.get("id") or args.get("key") or "").strip()
    if not proj:
        return {"ok": False, "error": _MISSING_PROJECT}
    if not iid:
        return {"ok": False, "error": _MISSING_IID}
    body: dict = {}
    if args.get("title"):
        body["title"] = args["title"]
    if args.get("description") is not None:
        body["description"] = args["description"]
    if args.get("labels") is not None:
        labels = args["labels"]
        if isinstance(labels, (list, tuple)):
            labels = ",".join(str(x) for x in labels)
        body["labels"] = labels
    if args.get("state_event"):
        body["state_event"] = args["state_event"]
    if isinstance(args.get("fields"), dict):
        body.update(args["fields"])
    enc, ierr = _iid_or_error(iid)
    if ierr:
        # BEFORE the empty-body check: a malformed iid with no fields reported
        # "no fields to update", which is true and useless.
        return ierr
    if not body:
        return {"ok": False, "error": "no fields to update"}
    r = _request("PUT", f"/projects/{_enc_proj(proj)}/issues/"
                 f"{enc}", body=body)
    if not r["ok"]:
        return r
    d = r["data"] if isinstance(r["data"], dict) else {}
    return {"ok": True, "iid": d.get("iid") or iid, "url": d.get("web_url"),
            "written": {k: args[k] for k in ("title", "description", "labels",
                        "state_event") if args.get(k) is not None}}


def gitlab_comment(args: dict, _cwd: str | None = None) -> dict:
    """Add a comment (note) to an issue. Required: ``project`` + ``iid``,
    ``body``."""
    proj = _proj_id(args)
    iid = str(args.get("iid") or args.get("id") or args.get("key") or "").strip()
    if not proj:
        return {"ok": False, "error": _MISSING_PROJECT}
    if not iid:
        return {"ok": False, "error": _MISSING_IID}
    if not args.get("body"):
        return {"ok": False, "error": "missing 'body'"}
    enc, ierr = _iid_or_error(iid)
    if ierr:
        return ierr
    r = _request("POST", f"/projects/{_enc_proj(proj)}/issues/"
                 f"{enc}/notes", body={"body": args["body"]})
    if not r["ok"]:
        return r
    d = r["data"] if isinstance(r["data"], dict) else {}
    return {"ok": True, "iid": iid, "id": d.get("id"),
            "written": {"comment": str(args["body"])[:2000]}}


def gitlab_mr_create(args: dict, _cwd: str | None = None) -> dict:
    """Open a merge request. Required: ``project`` (or GITLAB_PROJECT),
    ``source_branch``, ``title``. Optional: ``target_branch`` (default 'main'),
    ``description``, ``labels`` (list/csv), ``remove_source_branch``."""
    proj = _proj_id(args)
    src = str(args.get("source_branch") or args.get("source") or "").strip()
    if not proj:
        return {"ok": False, "error": _MISSING_PROJECT}
    if not src:
        return {"ok": False, "error": "missing 'source_branch'"}
    if not args.get("title"):
        return {"ok": False, "error": "missing 'title'"}
    body: dict = {"source_branch": src,
                  "target_branch": str(args.get("target_branch") or "main"),
                  "title": args["title"]}
    if args.get("description"):
        body["description"] = args["description"]
    if args.get("labels"):
        labels = args["labels"]
        body["labels"] = ",".join(str(x) for x in labels) if isinstance(labels, (list, tuple)) else labels
    if args.get("remove_source_branch") is not None:
        body["remove_source_branch"] = bool(args["remove_source_branch"])
    r = _request("POST", f"/projects/{_enc_proj(proj)}/merge_requests", body=body)
    if not r["ok"]:
        return r
    d = r["data"] if isinstance(r["data"], dict) else {}
    return {"ok": True, "iid": d.get("iid"), "url": d.get("web_url"),
            "written": {"title": args.get("title"),
                        "source": src, "target": body["target_branch"]}}


def gitlab_mr_comment(args: dict, _cwd: str | None = None) -> dict:
    """Comment on a merge request. Required: ``project`` + ``iid`` + ``body``."""
    proj = _proj_id(args)
    iid = str(args.get("iid") or args.get("id") or "").strip()
    if not proj or not iid:
        return {"ok": False, "error": "missing 'project'/'iid'"}
    if not args.get("body"):
        return {"ok": False, "error": "missing 'body'"}
    enc, ierr = _iid_or_error(iid)
    if ierr:
        return ierr
    r = _request("POST", f"/projects/{_enc_proj(proj)}/merge_requests/"
                 f"{enc}/notes", body={"body": args["body"]})
    if not r["ok"]:
        return r
    return {"ok": True, "iid": iid, "written": {"comment": str(args["body"])[:2000]}}


def gitlab_test() -> dict:
    """Connectivity + auth check for the Settings UI. Hits ``/user`` and, on
    auth failure, explains the most likely cause."""
    if not _configured():
        return {"ok": False, "error": "gitlab_not_configured"}
    scheme = _auth_scheme()
    r = _request("GET", "/user")
    if r.get("ok"):
        d = r["data"] if isinstance(r["data"], dict) else {}
        return {"ok": True, "base_url": _base(), "auth": scheme,
                "user": d.get("username") or d.get("name")}
    err = str(r.get("error", ""))
    out = {**r, "auth": scheme, "base_url": _base()}
    if err.startswith(("http 401", "http 403")):
        out["hint"] = ("Token rejected. Use a GitLab Personal/Project/Group "
                       "Access Token with at least 'read_api' (and 'api' for "
                       "writes) scope, not expired; Base URL must be the host "
                       "root (no /api/v4). For an OAuth token, enable the OAuth "
                       "(Bearer) option.")
    return out


# ─────────────────────────── pipelines / CI ─────────────────────────

# GitLab's pipeline status vocabulary, as documented for the list endpoint.
# TERMINAL = it will not change on its own; ACTIVE = still moving.
#
# `manual` is terminal ON PURPOSE: the pipeline is blocked on a job somebody
# has to click, so a watch that treated it as active would burn its whole
# budget waiting for a human. It is reported as blocked, never as success.
#
# An UNRECOGNISED status is treated as ACTIVE, never as terminal-success.
# GitLab adds statuses, and the failure modes are not symmetric: keep-watching
# costs one more poll, while "unknown means done" tells the user their deploy
# passed. (`canceling` and `waiting_for_callback` were both missing from an
# earlier version of these sets — which also made the gitlab_pipelines filter
# refuse a query GitLab would happily have answered.)
_TERMINAL = frozenset({"success", "failed", "canceled", "cancelled",
                       "skipped", "manual"})
_ACTIVE = frozenset({"created", "waiting_for_resource", "waiting_for_callback",
                     "preparing", "pending", "running", "scheduled",
                     "canceling"})
# `scope` is a different axis from `status` and the API accepts both.
_SCOPES = frozenset({"running", "pending", "finished", "branches", "tags"})

# Longest job log tail handed back. The chat loop caps ONE observation, and
# these tools are in `_shell._READ_OBS_TOOLS` so that cap is 80k rather than
# 6k — without that, the `jobs` array alone pushed the log out of the window.
_LOG_TAIL = 3000
# What we are willing to PULL for one job log. The shared default (200k) is
# sized for issue bodies, and `data[-tail:]` over a capped read returns the
# MIDDLE of a long log: npm/Gradle/Docker/pytest -v routinely blow past 200k,
# so the tail — the part that says why it failed — was exactly what got
# dropped, silently.
_TRACE_FETCH_CAP = 3_000_000
# Failed jobs whose log is fetched. Each is an extra HTTP round trip, and a
# matrix build can fail forty jobs for one reason.
_MAX_TRACES = 3
# Job pages to walk (100 per page — GitLab's maximum). A monorepo fan-out can
# exceed one page, and the symptom of silently taking page 1 is the worst kind:
# status "failed", failed_jobs [], no logs, no explanation.
_MAX_JOB_PAGES = 5

# Errors that will fail identically forever. Everything else — including a
# transport blip, which is the single most likely failure in a ten-minute
# watch of a self-hosted GitLab — is worth another poll. Enumerating the
# RETRYABLE set instead (429/5xx by string prefix) missed every URLError,
# TimeoutError and OSError, because those come back as `str(exc)`.
# NOTE `no_pipelines` is deliberately NOT here. "push, then watch the branch"
# is the advertised use case, and GitLab routinely has not created the pipeline
# at the moment of the first poll (webhook lag, `rules:` evaluation, a mirrored
# repo). Waiting and looking again is the entire remedy — treating it as fatal
# killed the watch on check 1 for exactly the thing it was built for.
_FATAL_ERRORS = ("gitlab_not_configured", "http 400", "http 401", "http 403",
                 "http 404", "unexpected_payload", "bad pipeline_id",
                 _MISSING_PROJECT)


def _is_fatal(res: dict) -> bool:
    err = str((res or {}).get("error") or "")
    return any(err.startswith(f) for f in _FATAL_ERRORS)


def _pipe_int(args: dict, key: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(args.get(key, default))
    except (TypeError, ValueError):
        v = default
    return max(lo, min(hi, v))


def _iid_or_error(iid) -> "tuple[str, dict | None]":
    """(encoded_iid, error_envelope).

    The SAME hole `_enc_id` documents, on the WRITE verbs: ``quote`` defaults
    to ``safe="/"``, so an iid of ``../../../../users/1/notes`` retargeted the
    request at another endpoint with the PRIVATE-TOKEN attached — and
    gitlab_comment / gitlab_update POST there.

    HARD ERROR, not a quiet escape-and-send. Escaping is enough to stop the
    traversal, but two of these call sites are approval-gated writes: sending
    a malformed iid anyway burns a human Approve and an authenticated POST to
    earn a 404, from which the model concludes "that issue doesn't exist"
    rather than "your iid was malformed".
    """
    enc, err = _enc_id(iid)
    if err:
        return "", {"ok": False, "error": f"bad iid: {err}",
                    "hint": "a GitLab issue/MR iid is the #number from the UI"}
    return enc, None


def _enc_id(value) -> "tuple[str, str | None]":
    """(encoded_id, error). GitLab ids are integers, so anything else is a
    typo or an injection — and only ONE of those is worth being lenient about.

    ``urllib.parse.quote`` defaults to ``safe="/"``, so a bare quote() leaves
    both ``/`` and ``.`` untouched: a model-supplied id of
    ``../../../../admin/ci/variables`` becomes a GET to an arbitrary path on
    the GitLab host, carrying the PRIVATE-TOKEN header. Nothing between the
    model and this function coerces argument types (the CATALOG types are
    advisory hints for native tool-calling, and the text ARGS_JSON path parses
    raw JSON), and a prompt-injected MR description is a realistic source of
    the value. Numeric-only is the fix; safe="" is the belt.
    """
    if isinstance(value, float) and value.is_integer():
        # JSON has one number type, so an id can arrive as 12.0 through the
        # ARGS_JSON path this function exists to defend.
        value = int(value)
    raw = str(value).strip()
    # isascii AND isdigit: str.isdigit() is True for '١٢٣' and '²'. safe=""
    # already neutralises those, but a rule should say what it means.
    if not (raw.isascii() and raw.isdigit()):
        return "", f"id must be a number, got {raw[:60]!r}"
    return urllib.parse.quote(raw, safe=""), None


def _pipe_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def _pipeline_summary(d: dict) -> dict:
    """Compact view of one pipeline."""
    if not isinstance(d, dict):
        return {}
    return {
        "id": d.get("id"),
        "iid": d.get("iid"),
        "status": d.get("status"),
        "ref": d.get("ref"),
        # BOTH. The list endpoint's `sha` filter is an exact match on the
        # full 40-char hash, so a caller feeding the displayed value back got
        # `no_pipelines` for a commit that plainly has one; a full hash in
        # every row of a listing is also noise. Keep the usable one and the
        # readable one, and say which is which.
        "sha": d.get("sha") or "",
        "sha_short": (d.get("sha") or "")[:12],
        "source": d.get("source"),
        "created_at": d.get("created_at"),
        "updated_at": d.get("updated_at"),
        "duration_s": d.get("duration"),
        "url": d.get("web_url"),
    }


def _job_summary(d: dict) -> dict:
    if not isinstance(d, dict):
        return {}
    return {
        "id": d.get("id"),
        "name": d.get("name"),
        "stage": d.get("stage"),
        "status": d.get("status"),
        "duration_s": d.get("duration"),
        # A job that failed with allow_failure did NOT fail the pipeline.
        # Reporting it as the cause sends someone to debug a job that is
        # working as configured.
        "allow_failure": bool(d.get("allow_failure")),
        "failure_reason": d.get("failure_reason"),
        "url": d.get("web_url"),
    }


def _read_pipeline(proj: str, pid) -> "tuple[dict | None, dict | None]":
    """(pipeline, error) for one pipeline id."""
    enc, err = _enc_id(pid)
    if err:
        return None, {"ok": False, "error": f"bad pipeline_id: {err}"}
    r = _request("GET", f"/projects/{_enc_proj(proj)}/pipelines/{enc}")
    if not r["ok"]:
        return None, r
    d = r["data"]
    if not isinstance(d, dict):
        return None, {"ok": False, "error": "unexpected_payload",
                      "hint": "GitLab returned a non-object for a pipeline"}
    if d.get("id") in (None, ""):
        # The list row is guarded for this; without the same guard here, a
        # 200-OK error body from a proxy/WAF ({"message": "403 Forbidden"})
        # summarises to all-None, the watch never pins an id, and it silently
        # re-resolves "latest on this ref" — onto a colleague's later push,
        # whose success it then reports as yours.
        return None, {"ok": False, "error": "unexpected_payload",
                      "hint": "GitLab returned a pipeline object with no id"}
    return d, None


def _resolve_pipeline(proj: str, args: dict) -> "tuple[dict | None, dict | None]":
    """(pipeline, error). Accepts an explicit ``id``/``pipeline_id``, or finds
    the LATEST pipeline for a ``ref``/``sha``, or the latest for the project.

    NOT RECURSIVE. It used to call itself after the list lookup, and a row
    without an ``id`` sent it round forever — the fallback saw pid=None, ran
    the same list query and recursed again, ~1000 HTTP calls deep, then raised
    RecursionError straight through a module whose header promises it never
    raises into the agent loop.
    """
    pid = args.get("pipeline_id") or args.get("id")
    if pid not in (None, "", 0):
        return _read_pipeline(proj, pid)
    params: dict = {"per_page": 1, "order_by": "id", "sort": "desc"}
    ref = (args.get("ref") or args.get("branch") or "").strip()
    sha = (args.get("sha") or args.get("commit") or "").strip()
    if ref:
        params["ref"] = ref
    if sha:
        params["sha"] = sha
    r = _request("GET", f"/projects/{_enc_proj(proj)}/pipelines", params=params)
    if not r["ok"]:
        return None, r
    rows = r["data"] if isinstance(r["data"], list) else []
    rows = [x for x in rows if isinstance(x, dict)]
    if not rows:
        if ref:
            where = f" for ref {ref!r}"
        elif sha:
            where = f" for sha {sha!r}"
        else:
            where = ""
        return None, {"ok": False, "error": "no_pipelines",
                      "hint": f"no pipeline found in {proj}{where}"}
    latest = rows[0].get("id")
    if latest in (None, ""):
        return None, {"ok": False, "error": "unexpected_payload",
                      "hint": "GitLab returned a pipeline row with no id"}
    # The list endpoint returns a SHORTER shape than the single-pipeline one
    # (no duration, no coverage). Re-read the full record so callers get the
    # same fields whether they passed an id or a ref.
    return _read_pipeline(proj, latest)


def gitlab_pipelines(args: dict, _cwd: str | None = None) -> dict:
    """READ: list recent CI pipelines. Optional ``project`` (defaults to
    GITLAB_PROJECT), ``ref``/``branch``, ``status``, ``sha``, ``limit``."""
    proj = _proj_id(args)
    if not proj:
        return {"ok": False, "error": _MISSING_PROJECT,
                "hint": "pass project=\"group/proj\" or set GITLAB_PROJECT"}
    params: dict = {"per_page": _pipe_int(args, "limit", 20, 1, 100),
                    "order_by": "id", "sort": "desc"}
    ref = (args.get("ref") or args.get("branch") or "").strip()
    if ref:
        params["ref"] = ref
    sha = (args.get("sha") or args.get("commit") or "").strip()
    if sha:
        params["sha"] = sha
    status = (args.get("status") or "").strip().lower()
    if status:
        # Send only what GitLab accepts: an unknown value is a 400 for the
        # whole call, which reads to the agent as "GitLab is broken".
        if status not in _TERMINAL and status not in _ACTIVE:
            return {"ok": False, "error": f"unknown status {status!r}",
                    "hint": "one of: " + ", ".join(sorted(_ACTIVE | _TERMINAL))}
        params["status"] = "canceled" if status == "cancelled" else status
    r = _request("GET", f"/projects/{_enc_proj(proj)}/pipelines", params=params)
    if not r["ok"]:
        return r
    rows = r["data"] if isinstance(r["data"], list) else []
    return {"ok": True, "project": proj,
            "pipelines": [_pipeline_summary(x) for x in rows]}


def _fetch_jobs(proj: str, pipeline_id) -> "tuple[list, dict | None, str]":
    """(jobs, error, note). Walks pages — GitLab caps per_page at 100, and one
    silent page of a 120-job fan-out shows `failed` with nothing failing in it.

    NOTE: `include_retried` defaults to false on this endpoint, so a retried
    job appears once and `logs` keyed by job name cannot collide. Turning it on
    would silently break both — don't, without fixing them.
    """
    enc, err = _enc_id(pipeline_id)
    if err:
        return [], {"ok": False, "error": f"bad pipeline_id: {err}"}, ""
    out: list = []
    for page in range(1, _MAX_JOB_PAGES + 1):
        r = _request("GET", f"/projects/{_enc_proj(proj)}/pipelines/{enc}/jobs",
                     params={"per_page": 100, "page": page})
        if not r["ok"]:
            if not out:
                return out, r, ""
            # Page 1 landed, page 2 blipped. Handing back 100 jobs with no
            # error and no note presents a PARTIAL list as the complete one —
            # so a pipeline whose only failing job was on page 2 reads as
            # having nothing wrong with it.
            return out, None, (f"job listing stopped after {len(out)} jobs "
                               f"({r.get('error')}) — a later job may be "
                               f"missing from this list")
        rows = r["data"] if isinstance(r["data"], list) else []
        out.extend(_job_summary(x) for x in rows if isinstance(x, dict))
        if len(rows) < 100:
            return out, None, ""
    # "at least": a 5th page of exactly 100 is indistinguishable from a 6th
    # page existing, and claiming "more than 500" for exactly 500 tells a user
    # data is missing when it is not.
    return out, None, (f"at least {_MAX_JOB_PAGES * 100} jobs — listing stops "
                       f"there, so a later job may be missing")


def _fetch_bridges(proj: str, pipeline_id) -> list:
    """Trigger (bridge) jobs — the child pipelines a parent triggered.

    The jobs endpoint does NOT return these. In a monorepo, a parent that is
    `failed` purely because a `trigger:`-ed child failed therefore reported
    `failed_jobs: []` with no logs: the same silent no-cause symptom as a
    truncated page, and the normal topology rather than an edge case.
    """
    enc, err = _enc_id(pipeline_id)
    if err:
        return []
    r = _request("GET", f"/projects/{_enc_proj(proj)}/pipelines/{enc}/bridges",
                 params={"per_page": 100})
    if not r["ok"]:
        return []
    rows = r["data"] if isinstance(r["data"], list) else []
    out = []
    for b in rows:
        if not isinstance(b, dict):
            continue
        child = b.get("downstream_pipeline")
        # isinstance, not `or {}` — that guards falsy, not non-dict, and a
        # string here reached `.get` and raised AttributeError straight through
        # a module whose header promises it never raises into the agent loop.
        if not isinstance(child, dict):
            child = {}
        out.append({"name": b.get("name"), "status": b.get("status"),
                    "allow_failure": bool(b.get("allow_failure")),
                    "child_pipeline_id": child.get("id"),
                    "child_status": child.get("status"),
                    "child_url": child.get("web_url")})
    return out


_ANSI_RE = None


def _clean_log(txt: str) -> str:
    """Strip ANSI colour and GitLab's section markers.

    Both are pure noise, and both eat the tail budget: a coloured pytest run
    can spend a third of 3000 characters on escape sequences that say nothing
    about why it failed.
    """
    global _ANSI_RE
    if _ANSI_RE is None:
        import re as _re
        _ANSI_RE = _re.compile(
            r"\x1b\[[0-9;?]*[ -/]*[@-~]|"
            # Consume the trailing CR: GitLab writes `section_end:…:name\r`,
            # and stopping before it left a log of nothing but "\r\r".
            r"section_(?:start|end):\d+:[^\r\n]*\r?")
    return _ANSI_RE.sub("", txt)


def _job_trace(proj: str, job_id, tail: int = _LOG_TAIL) -> "tuple[str, str]":
    """(text, note). The END of one job's log — or an honest admission that it
    is not the end.

    The trace endpoint returns plain TEXT, not JSON. It is pulled with its own
    large body cap (see _TRACE_FETCH_CAP), because the shared 200k default is
    sized for issue bodies and `data[-tail:]` over a capped READ returns the
    MIDDLE of a long log.

    RAISING THE CAP IS NOT THE SAME AS FIXING IT. http_request keeps the HEAD
    of an over-cap body, so above the cap this is still the middle — the fix is
    that http_request now REPORTS truncation and this says so, instead of
    printing a confident "showing the last 3000 of 3000000 chars" over a slice
    that does not contain the failure. Better a note the reader can act on than
    a number that is wrong in the direction of reassurance.
    """
    enc, err = _enc_id(job_id)
    if err:
        return "", f"log unavailable (bad job id: {err})"
    r = _request("GET", f"/projects/{_enc_proj(proj)}/jobs/{enc}/trace",
                 body_cap=_TRACE_FETCH_CAP, parse_json=False)
    if not r["ok"]:
        # A failed job whose log cannot be read must not vanish silently —
        # "it failed and there is no log" is itself the finding.
        return "", f"log unavailable ({r.get('error')})"
    data = r["data"]
    if not isinstance(data, str):
        return "", "log unavailable (unexpected payload)"
    txt = _clean_log(data)
    if not txt.strip():
        # Checked AFTER cleaning: a trace of nothing but section markers is
        # non-blank raw and empty once they are stripped, and "\r\r" as a
        # failure log is worse than saying there is nothing there. A real,
        # distinct fact — and the common one at the exact moment the watch's
        # terminal re-read fires, before the runner has flushed.
        return "", "the job log is empty"
    if r.get("body_cap_hit"):
        # We have the START of a log bigger than we were willing to fetch. The
        # tail of THIS is not the tail of the job.
        return txt[:tail], (
            f"THIS IS NOT THE END OF THE LOG — it is larger than the "
            f"{r['body_cap_hit']}-byte fetch cap, so this is the first "
            f"{tail} chars. Open the job URL for the failure.")
    note = ""
    if len(txt) > tail:
        note = f"showing the last {tail} of {len(txt)} chars"
    return txt[-tail:], note


def _truthy(val, default: bool = True) -> bool:
    """`{"logs": "false"}` from a local model is a STRING, and a bare truthiness
    test reads it as yes — here that means paying HTTP round trips the caller
    explicitly declined."""
    if val is None:
        return default
    if isinstance(val, str):
        if not val.strip():
            return default          # "" is UNSET, not "no"
        return val.strip().lower() not in ("false", "0", "no", "off")
    return bool(val)


def _pipeline_status_flags(out: dict, status: str) -> None:
    out["finished"] = status in _TERMINAL
    out["passed"] = status == "success"
    if status == "manual":
        out["blocked_on_manual"] = True
        out["hint"] = ("pipeline is waiting for a manual job — it will not "
                       "progress until someone runs it")


def _failed_job_logs(proj, args: dict, failed: list) -> tuple[dict, str]:
    """``(logs by job name, truncation note)`` for the failed jobs."""
    tail = _pipe_int(args, "log_chars", _LOG_TAIL, 200, 20_000)
    logs: dict = {}
    for j in failed[:_MAX_TRACES]:
        txt, note = _job_trace(proj, j.get("id"), tail)
        logs[str(j.get("name"))] = txt or f"({note or 'no log'})"
        if note and txt:
            logs[str(j.get("name")) + " [note]"] = note
    # Never let a cap look like completeness.
    truncated = (f"{len(failed)} jobs failed; showing logs for the first "
                 f"{_MAX_TRACES}") if len(failed) > _MAX_TRACES else ""
    return logs, truncated


def _explain_failure(out: dict, proj, failed: list) -> None:
    """Something failed and it was not any job we can see. Say that, rather than
    hand back an empty list that reads as "nothing failed"."""
    bridges = _fetch_bridges(proj, out.get("id"))
    bad = [b for b in bridges
           if str(b.get("status")).lower() in ("failed", "canceled",
                                               "cancelled", "canceling")
           and not b.get("allow_failure")]
    if bad:
        # Reported even when a job ALSO failed: a parent can fail for both
        # reasons, and only mentioning the child when nothing else failed hid it
        # in exactly the messier case.
        out["failed_child_pipelines"] = bad
        if not failed:
            out["hint"] = ("this pipeline failed because a TRIGGERED CHILD "
                           "pipeline failed — read that one for the cause")
    elif out.get("jobs_error"):
        # We did not READ the jobs, so an empty list is UNREAD, not empty — and
        # offering three speculative causes while the actual error sits two keys
        # above sends the reader to debug a .gitlab-ci.yml that is perfectly fine.
        out["hint"] = (f"pipeline is failed, and the job list could not be "
                       f"read ({out['jobs_error']}) — the cause is not known "
                       f"from this result, not absent from it")
    elif not failed:
        out["hint"] = ("pipeline is failed but no failed job was found: it may "
                       "be a trigger/bridge failure, a job outside the listed "
                       "pages, or a pipeline-level error (e.g. an invalid "
                       ".gitlab-ci.yml)")


def _collect_jobs(out: dict, proj, status: str) -> tuple[list, list, bool]:
    """``(jobs, failed_jobs, keep_going)``.

    On a job-list error the pipeline itself read fine — say so rather than
    losing it. But do NOT stop early on a FAILED pipeline: that skipped the
    bridge check and the "no failed job found" hint, leaving `status: failed`
    with no explanation at all, which is the symptom, not the fix.
    """
    jobs, jerr, jnote = _fetch_jobs(proj, out.get("id"))
    if jerr:
        out["jobs_error"] = jerr.get("error")
        if status != "failed":
            return [], [], False
        return [], [], True
    # allow_failure jobs did not fail the pipeline — they are kept out of the
    # blamed list, and out of the round trips we spend on logs.
    failed = [j for j in jobs
              if str(j.get("status")).lower() == "failed"
              and not j.get("allow_failure")]
    if jnote:
        out["jobs_truncated"] = jnote
    return jobs, failed, True


def gitlab_pipeline(args: dict, _cwd: str | None = None, *,
                    skip_jobs: bool = False) -> dict:
    """READ one CI pipeline: status, jobs, and the log tail of what failed.

    Address it by ``pipeline_id``, or by ``ref``/``branch`` (latest pipeline on
    that branch), or by ``sha`` — or omit all three for the project's latest.
    Set ``logs`` false to skip fetching failed-job logs."""
    proj = _proj_id(args)
    if not proj:
        return {"ok": False, "error": _MISSING_PROJECT,
                "hint": "pass project=\"group/proj\" or set GITLAB_PROJECT"}
    d, err = _resolve_pipeline(proj, args)
    if err:
        return err
    out = {"ok": True, "project": proj, **_pipeline_summary(d or {})}
    status = str(out.get("status") or "").lower()
    _pipeline_status_flags(out, status)
    if skip_jobs:
        # A PARAMETER, not a key in `args`. As a key it was model-injectable:
        # `_loop` hands the raw parsed args to the tool, the schema allows
        # additional properties, and the wrapper passes them straight through —
        # so a prompt-injected `"_skip_jobs": true` produced a failed pipeline
        # with no failed_jobs, no logs, no bridge check and no hint. A signature
        # the model cannot reach is the way to be sure.
        out["jobs_omitted"] = "polling snapshot — jobs and logs not fetched"
        return out
    jobs, failed, keep_going = _collect_jobs(out, proj, status)
    if not keep_going:
        return out
    out["failed_jobs"] = [j.get("name") for j in failed]
    # LOGS BEFORE JOBS in the dict. json.dumps preserves insertion order and the
    # loop truncates the serialised observation, so a 40-job `jobs` array sitting
    # in front of `logs` sliced the failure reason out of what the model actually
    # reads — at 14 jobs, measured.
    if failed and _truthy(args.get("logs")):
        out["logs"], truncated = _failed_job_logs(proj, args, failed)
        if truncated:
            out["logs_truncated"] = truncated
    if status == "failed":
        _explain_failure(out, proj, failed)
    out["jobs"] = jobs
    return out


class _WatchBudget:
    """How long one pipeline watch may run, and whether Stop can reach it."""

    __slots__ = ("interval", "budget", "max_checks", "sid", "chat_cancel",
                 "unattended")

    def __init__(self, args: dict) -> None:
        self.interval = _pipe_int(args, "interval_s", 20, 5, 3600)
        # This SLEEPS inside one tool call on the producer thread: the step cap,
        # the turn deadline and mid-run steering are all checked BETWEEN steps,
        # so none of them bound it, and there are only eight producer slots.
        # Same discipline and the same ceiling as watch_until.
        self.budget = _pipe_int(args, "timeout_s", 600, 10,
                                _pipe_env("AIFORGE_GITLAB_WATCH_MAX_SECONDS", 1800))
        self.max_checks = _pipe_int(args, "max_checks", 60, 1,
                                    _pipe_env("AIFORGE_GITLAB_WATCH_MAX_CHECKS", 200))
        self.chat_cancel = None
        self.sid = None
        try:
            from aiforge_core.runtime import chat_cancel as _cc
            self.chat_cancel = _cc
            self.sid = _cc.active()
        except Exception:  # noqa: BLE001 — no cancel machinery → unattended
            self.sid = None
        self.unattended = self.sid is None
        if self.unattended:
            # No cancel handle at all: the jobs runner and /api/chat/agent pass
            # session_id=None, and chat_cancel is a ContextVar that does not
            # cross into a worker thread. NOTHING can interrupt this loop, so it
            # does not get a long one — fail SHORT, not open. Reported back,
            # because a caller that asked for 600s and silently got 180 cannot
            # explain its own timeout.
            #
            # NOT the team/ADK path: chat_pipeline._drive and
            # parallel_subtasks._stream both call `chat_cancel.set_active(...)`
            # inside their driver thread precisely so Stop reaches the tools they
            # run. Those get a real sid, so they are attended and keep the full
            # budget — which is why the doer wrapper's docstring had to stop
            # promising a clamp.
            self.budget = min(self.budget, _pipe_env(
                "AIFORGE_GITLAB_WATCH_UNATTENDED_SECONDS", 180))
            self.max_checks = min(self.max_checks, 10)

    def cancelled(self) -> bool:
        return not self.unattended and self.chat_cancel.is_cancelled(self.sid)


def _watch_envelope(checks: int, started: float) -> dict:
    # No `requests` key. It counted gitlab_pipeline CALLS, so it was always
    # `checks` or `checks + 1` — carrying no information beyond `checks` while
    # its name implied HTTP volume, which is 1-20x higher.
    import time as _time
    return {"checks": checks,
            "elapsed_s": round(_time.monotonic() - started, 1)}


def _watch_stopped(state: dict, checks: int, started: float, err: dict) -> dict:
    # SPREAD FIRST, explicit keys last. The other order let `last["ok"]`
    # overwrite `ok: False`, so a watch the user stopped came back as a watch
    # that succeeded — and an error dict overwrote "stopped by user" with an
    # HTTP error, making Stop indistinguishable from a failure.
    out = {**state, **_watch_envelope(checks, started),
           "ok": False, "stopped": True, "error": "stopped by user"}
    if err and state:
        # The snapshot is real but old — the same thing the timeout path says,
        # and for the same reason.
        out["stale"] = True
        out["last_poll_error"] = err.get("error")
    return out


def _watch_finished(args: dict, cwd, res: dict, pinned, checks: int,
                    started: float) -> dict:
    """The pipeline is done — now, and only now, pay for the jobs and logs."""
    final = gitlab_pipeline({**args, "pipeline_id": pinned}, cwd)
    if final.get("ok"):
        return {**final, **_watch_envelope(checks, started)}
    # The ONE call that was going to fetch the logs failed (a 429 right at
    # completion is common). Falling back silently to the logs=False snapshot
    # hands back a clean, finished, failed result naming a job with no log and
    # no reason there is no log — which reads as "there was no log".
    return {**res, **_watch_envelope(checks, started),
            "logs_error": final.get("error"),
            "hint": ("the pipeline finished, but re-reading it for the job logs "
                     "failed — read it again with gitlab_pipeline")}


def _watch_fatal(good: dict, res: dict, checks: int, started: float) -> dict:
    """A bad token or a missing project fails identically forever; looping on it
    burns the whole budget to learn nothing.

    But we may already have READ this pipeline (a token rotated mid-watch, a
    project archived). The docstring promises `passed` on every return that
    observed the pipeline at all — discarding `good` here broke that promise on
    the one path that had the data and dropped it.
    """
    out = {**good, **res, **_watch_envelope(checks, started)}
    if good:
        out["stale"] = True
        out["last_poll_error"] = res.get("error")
    return out


def _watch_sleep(b: "_WatchBudget") -> bool:
    """Sleep one interval in slices so Stop is honoured mid-wait, not after it.
    False when the watch was cancelled during the wait."""
    import time as _time
    waited = 0.0
    while waited < b.interval:
        if b.cancelled():
            return False
        _time.sleep(min(1.0, b.interval - waited))
        waited += 1.0
    return True


def _watch_timeout(b: "_WatchBudget", good: dict, err: dict, checks: int,
                   started: float) -> dict:
    tail = {**_watch_envelope(checks, started), "timed_out": True}
    if b.unattended:
        # Both halves, and only the one that BIT. Reporting the seconds budget
        # unconditionally read as "the 180s ran out" when what actually ended the
        # run was the 10-check cap at 45s — the same complaint about a silently
        # shortened budget, one field over.
        if checks >= b.max_checks:
            tail["unattended_max_checks"] = b.max_checks
        else:
            tail["unattended_budget_s"] = b.budget
    if not good:
        # We never once read the pipeline. Saying ok:True here handed the agent a
        # successful-looking envelope carrying an HTTP error and no `passed` key
        # at all — the one shape from which a model can tell a user the build
        # passed when nothing was ever observed.
        return {**(err or {"ok": False, "error": "no_successful_poll"}),
                **tail, "ok": False,
                "reason": "the pipeline was never successfully read"}
    out = {**good, **tail, "ok": True}
    if err:
        # Ended on a failed poll: the data is the last GOOD one, and it is old.
        out["stale"] = True
        out["last_poll_error"] = err.get("error")
    out["reason"] = (f"still {good.get('status') or 'unknown'} after "
                     f"{checks} check(s) — the watch gave up, "
                     f"the pipeline did not")
    return out


def _poll_args(args: dict, pinned) -> dict:
    """Args for ONE poll.

    ``logs=False`` while polling: a stage-1 failure with later stages still
    running re-downloaded up to three job traces on EVERY poll and threw all but
    the last away — fetched once, on the check that finishes. ``skip_jobs`` at
    the call site does the same for the job list: only ``finished`` decides
    whether to keep polling, and walking up to five 100-job pages every poll
    re-fetched — and discarded — six times the HTTP the watch actually needed.
    """
    out = {**args, "logs": False}
    if pinned:
        out["pipeline_id"] = pinned
    return out


def _one_check(args: dict, cwd, pinned, good: dict, _err: dict, checks: int,
               started: float):
    """One poll. Returns ``(final_result_or_None, good, err, pinned)``."""
    res = gitlab_pipeline(_poll_args(args, pinned), cwd, skip_jobs=True)
    if not res.get("ok"):
        if _is_fatal(res):
            return _watch_fatal(good, res, checks, started), good, res, pinned
        return None, good, res, pinned
    # PIN the id after the first successful resolve. A ref-addressed watch
    # re-ran "latest pipeline on this ref" every poll, so a colleague pushing
    # mid-watch silently re-targeted it — and it would then report `passed` for
    # a pipeline the user never asked about while theirs failed.
    pinned = pinned or res.get("id")
    if res.get("finished"):
        return (_watch_finished(args, cwd, res, pinned, checks, started),
                res, {}, pinned)
    return None, res, {}, pinned


def gitlab_pipeline_watch(args: dict, cwd: str | None = None) -> dict:
    """WATCH one CI pipeline until it finishes (or the budget runs out).

    ONE tool call covers the whole watch — no model request per poll, which is
    the entire point now that model calls are rate-ceilinged. Same addressing
    as :func:`gitlab_pipeline`. Optional ``interval_s`` (default 20),
    ``timeout_s`` (default 600), ``max_checks``.

    Returns ``checks`` and ``elapsed_s`` always. A run that ENDS on a finished
    pipeline returns the full ``gitlab_pipeline`` shape (jobs, failed_jobs,
    logs); a run that is stopped, gives up, or hits a fatal error returns the
    last snapshot it read — which was polled without jobs or logs, so those
    keys are absent. ``ok`` says the WATCH worked; whether the pipeline passed
    is ``passed``, present on every return that observed the pipeline at all.
    """
    import time as _time
    if not _proj_id(args):
        return {"ok": False, "error": _MISSING_PROJECT,
                "hint": "pass project=\"group/proj\" or set GITLAB_PROJECT"}
    b = _WatchBudget(args)
    started = _time.monotonic()
    checks = 0
    pinned = args.get("pipeline_id") or args.get("id") or None
    good: dict = {}          # the last snapshot we actually READ
    err: dict = {}           # the last failed poll, if the run ended on one
    while checks < b.max_checks:
        if b.cancelled():
            return _watch_stopped(good, checks, started, err)
        checks += 1
        done, good, err, pinned = _one_check(args, cwd, pinned, good, err,
                                             checks, started)
        if done is not None:
            return done
        if (_time.monotonic() - started) + b.interval > b.budget \
                or checks >= b.max_checks:
            break
        if not _watch_sleep(b):
            return _watch_stopped(good, checks, started, err)
    return _watch_timeout(b, good, err, checks, started)


__all__ = ["gitlab_search", "gitlab_read", "gitlab_create", "gitlab_update",
           "gitlab_comment", "gitlab_mr_create", "gitlab_mr_comment",
           "gitlab_pipelines", "gitlab_pipeline", "gitlab_pipeline_watch",
           "gitlab_test"]
