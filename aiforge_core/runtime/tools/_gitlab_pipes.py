"""GitLab pipelines: status sets, reading and resolving a pipeline, and the
gitlab_pipelines tool."""
from __future__ import annotations

import os
import urllib.parse

_PROJECT_HINT = 'pass project="group/proj" or set GITLAB_PROJECT'
_MISSING_IID = "missing 'iid'"
_MISSING_PROJECT = "missing 'project'"


def _pkg():
    """``gitlab``, the module this code was split from, looked up on each call.

    Only names read through here follow a patch on ``gitlab``; patch any other
    name on this module."""
    import aiforge_core.runtime.tools.gitlab as package
    return package


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
    pkg = _pkg()
    enc, err = _enc_id(pid)
    if err:
        return None, {"ok": False, "error": f"bad pipeline_id: {err}"}
    r = pkg._request("GET", f"/projects/{pkg._enc_proj(proj)}/pipelines/{enc}")
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
    pkg = _pkg()
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
    r = pkg._request("GET", f"/projects/{pkg._enc_proj(proj)}/pipelines", params=params)
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
    pkg = _pkg()
    proj = pkg._proj_id(args)
    if not proj:
        return {"ok": False, "error": pkg._MISSING_PROJECT,
                "hint": pkg._PROJECT_HINT}
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
    r = pkg._request("GET", f"/projects/{pkg._enc_proj(proj)}/pipelines", params=params)
    if not r["ok"]:
        return r
    rows = r["data"] if isinstance(r["data"], list) else []
    return {"ok": True, "project": proj,
            "pipelines": [_pipeline_summary(x) for x in rows]}
