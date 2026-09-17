"""GitLab jobs: fetching jobs and bridges, cleaning traces, explaining a
failure, and the gitlab_pipeline tool."""
from __future__ import annotations

from ._gitlab_pipes import (
    _LOG_TAIL,
    _MAX_JOB_PAGES,
    _MAX_TRACES,
    _TERMINAL,
    _TRACE_FETCH_CAP,
    _enc_id,
    _job_summary,
    _pipe_int,
    _pipeline_summary,
    _resolve_pipeline,
)


def _pkg():
    """``gitlab``, the module this code was split from, looked up on each call.

    Only names read through here follow a patch on ``gitlab``; patch any other
    name on this module."""
    import aiforge_core.runtime.tools.gitlab as package
    return package


def _fetch_jobs(proj: str, pipeline_id) -> "tuple[list, dict | None, str]":
    """(jobs, error, note). Walks pages — GitLab caps per_page at 100, and one
    silent page of a 120-job fan-out shows `failed` with nothing failing in it.

    NOTE: `include_retried` defaults to false on this endpoint, so a retried
    job appears once and `logs` keyed by job name cannot collide. Turning it on
    would silently break both — don't, without fixing them.
    """
    pkg = _pkg()
    enc, err = _enc_id(pipeline_id)
    if err:
        return [], {"ok": False, "error": f"bad pipeline_id: {err}"}, ""
    out: list = []
    for page in range(1, _MAX_JOB_PAGES + 1):
        r = pkg._request("GET", f"/projects/{pkg._enc_proj(proj)}/pipelines/{enc}/jobs",
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
    pkg = _pkg()
    enc, err = _enc_id(pipeline_id)
    if err:
        return []
    r = pkg._request("GET", f"/projects/{pkg._enc_proj(proj)}/pipelines/{enc}/bridges",
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
    pkg = _pkg()
    enc, err = _enc_id(job_id)
    if err:
        return "", f"log unavailable (bad job id: {err})"
    r = pkg._request("GET", f"/projects/{pkg._enc_proj(proj)}/jobs/{enc}/trace",
                 body_cap=_TRACE_FETCH_CAP, parse_json=False)
    if not r["ok"]:
        # A failed job whose log cannot be read must not vanish silently —
        # "it failed and there is no log" is itself the finding.
        return "", f"log unavailable ({r.get('error')})"
    data = r["data"]
    if not isinstance(data, str):
        return "", "log unavailable (unexpected payload)"
    txt = pkg._clean_log(data)
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
    pkg = _pkg()
    proj = pkg._proj_id(args)
    if not proj:
        return {"ok": False, "error": pkg._MISSING_PROJECT,
                "hint": pkg._PROJECT_HINT}
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
