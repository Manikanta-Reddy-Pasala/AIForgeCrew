"""Scheduled-jobs routes (/api/jobs/*) — split out of api.py.

Self-contained: the request models + the croniter guard live here, and every
handler imports the jobs runtime (aiforge_core.jobs.*) function-locally, exactly
as it did inline in api.py. Behavior identical.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter()

_CRONITER_HINT = ("Scheduled jobs need the 'croniter' package — run "
                  "`uv pip install croniter` (or `uv sync`) and restart the API.")


class JobPreviewBody(BaseModel):
    instructions: str = Field(..., min_length=1)


class JobCreate(BaseModel):
    name: str = Field(..., min_length=1)
    # min_length=1, not 9 — croniter validates the real shape (and accepts
    # aliases like "@daily" that are shorter than a 5-field expression);
    # a length heuristic would false-reject those.
    cron: str = Field(..., min_length=1)
    ticket_title: str = Field(..., min_length=1)
    ticket_body: str = Field(..., min_length=1)
    project: str | None = None
    # 'ticket' (default) → the code pipeline builds it into a PR; 'agent' → runs
    # the request through the chat agent (full jira/confluence/email tools, no
    # code framing) for operational tasks ("read Jira + email me").
    kind: str = Field("ticket")


class JobScriptCreate(BaseModel):
    """Finalize a conversational job-builder session into a scheduled SCRIPT
    job: the approved script body + a cron. The script is written to the local
    jobs folder (~/.aiforge/jobs), NOT the repo — it's user data."""
    name: str = Field(..., min_length=1)
    cron: str = Field(..., min_length=1)
    script: str = Field(..., min_length=1)
    description: str | None = None


class JobPatch(BaseModel):
    name: str | None = None
    cron: str | None = None
    ticket_title: str | None = None
    ticket_body: str | None = None
    project: str | None = None
    enabled: bool | None = None


def _require_croniter() -> None:
    """503 with an actionable message when croniter isn't installed, instead
    of an opaque ModuleNotFoundError 500 that breaks the whole Jobs page."""
    from aiforge_core.jobs import parse as jobs_parse
    if not jobs_parse.CRONITER_AVAILABLE:
        raise HTTPException(503, _CRONITER_HINT)


@router.post("/api/jobs/preview")
def jobs_preview(payload: JobPreviewBody) -> dict:
    """NL instructions → parsed draft + human schedule + next runs.
    Saves NOTHING. Parse errors come back as {ok: False, error} so the
    UI renders them in the preview card instead of a 500."""
    from aiforge_core.jobs import parse as jobs_parse
    if not jobs_parse.CRONITER_AVAILABLE:
        return {"ok": False, "error": _CRONITER_HINT}
    return jobs_parse.parse_instructions(payload.instructions)


@router.post("/api/jobs", status_code=201, responses={
    400: {"description": "Bad request"},
    503: {"description": "Service unavailable (croniter not installed)"}})
def jobs_create(payload: JobCreate) -> dict:
    from aiforge_core.jobs import parse as jobs_parse, store as jobs_store
    _require_croniter()
    # schedulable() rejects both invalid AND save-valid-but-unschedulable
    # crons (e.g. "0 0 31 2 *"), so next_runs below can't 500.
    if not jobs_parse.schedulable(payload.cron):
        raise HTTPException(400, f"invalid or unschedulable cron: {payload.cron!r}")
    nxt = jobs_parse.next_runs(payload.cron, n=1)[0]
    _kind = payload.kind if payload.kind in ("ticket", "agent") else "ticket"
    return jobs_store.create(
        name=payload.name, cron=payload.cron,
        ticket_title=payload.ticket_title, ticket_body=payload.ticket_body,
        project=payload.project, next_run_at=nxt, kind=_kind)


@router.post("/api/jobs/script", status_code=201, responses={
    400: {"description": "Bad request"},
    503: {"description": "Service unavailable (croniter not installed)"}})
def jobs_create_script(payload: JobScriptCreate) -> dict:
    """Finalize a script job: write the approved script to the local jobs
    folder and register a cron job that RUNS it (deterministic — no LLM per
    tick). This is the endpoint the conversational job builder calls once the
    user has dry-run and approved the script."""
    from aiforge_core.jobs import parse as jobs_parse
    from aiforge_core.jobs import scripts as jobs_scripts
    from aiforge_core.jobs import store as jobs_store
    _require_croniter()
    if not jobs_parse.schedulable(payload.cron):
        raise HTTPException(400, f"invalid or unschedulable cron: {payload.cron!r}")
    try:
        script_path = jobs_scripts.write_script(payload.name, payload.script)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    nxt = jobs_parse.next_runs(payload.cron, n=1)[0]
    body = payload.description or f"Runs script: {script_path}"
    # UPSERT by name: re-finalizing a job of the SAME name UPDATES it (new
    # schedule + script) instead of scheduling a SECOND duplicate that fires
    # alongside the original. (The old script file is left on disk — scripts are
    # kept for reuse by design — but only ONE job/schedule points at the new one.)
    existing = next((j for j in jobs_store.list_jobs()
                     if j.get("name") == payload.name
                     and (j.get("kind") or "ticket") == "script"), None)
    if existing:
        return jobs_store.update(
            existing["id"], cron=payload.cron, ticket_body=body,
            next_run_at=nxt, script_path=script_path, last_error=None) or existing
    return jobs_store.create(
        name=payload.name, cron=payload.cron,
        ticket_title=payload.name, ticket_body=body,
        project=None, next_run_at=nxt, kind="script", script_path=script_path)


@router.get("/api/jobs")
def jobs_list() -> list[dict]:
    from aiforge_core.jobs import parse as jobs_parse, store as jobs_store
    out = jobs_store.list_jobs()
    for j in out:
        j["human_schedule"] = jobs_parse.human_schedule(j["cron"])
    return out


@router.patch("/api/jobs/{job_id}", responses={400: {"description": "Bad request"}, 404: {"description": "Not found"}})
def jobs_patch(job_id: int, payload: JobPatch) -> dict:
    from aiforge_core.jobs import parse as jobs_parse, store as jobs_store
    if jobs_store.get(job_id) is None:
        raise HTTPException(404, f"job {job_id} not found")
    _require_croniter()
    fields = {k: v for k, v in payload.model_dump().items() if v is not None}
    # Reject blanking a required text field (JobPatch has no min_length, so
    # {"ticket_title": ""} would otherwise store an empty value that later
    # fires an empty-title ticket).
    for k in ("name", "ticket_title", "ticket_body"):
        if k in fields and not str(fields[k]).strip():
            raise HTTPException(400, f"{k} cannot be empty")
    if "cron" in fields:
        if not jobs_parse.schedulable(fields["cron"]):
            raise HTTPException(400,
                                f"invalid or unschedulable cron: {fields['cron']!r}")
        fields["next_run_at"] = jobs_parse.next_runs(fields["cron"], n=1)[0]
    return jobs_store.update(job_id, **fields)


@router.delete("/api/jobs/{job_id}", responses={404: {"description": "Not found"}})
def jobs_delete(job_id: int) -> dict:
    from aiforge_core.jobs import store as jobs_store
    # Deleting the row IS deleting the schedule — the scheduler only ever
    # fires rows `due_jobs()` returns, so a removed row can never fire again
    # (there's no separate OS crontab/systemd entry to also clean up). The
    # script FILE is left on disk on purpose: it's user-authored/approved
    # content the operator may want to reuse for a new job later, not
    # scheduler-owned state.
    if not jobs_store.delete(job_id):
        raise HTTPException(404, f"job {job_id} not found")
    return {"ok": True}


@router.post("/api/jobs/{job_id}/run-now", responses={404: {"description": "Not found"}})
def jobs_run_now(job_id: int) -> dict:
    """Manual fire — same code path as the scheduler tick; works even
    when the job is paused."""
    from aiforge_core.jobs import scheduler as jobs_scheduler
    from aiforge_core.jobs import store as jobs_store
    job = jobs_store.get(job_id)
    if job is None:
        raise HTTPException(404, f"job {job_id} not found")
    _require_croniter()
    ok = jobs_scheduler.fire(job)
    return {"ok": ok, "job": jobs_store.get(job_id)}
