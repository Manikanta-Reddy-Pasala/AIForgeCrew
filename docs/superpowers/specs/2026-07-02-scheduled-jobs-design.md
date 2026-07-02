# Scheduled Jobs — Design

**Date:** 2026-07-02
**Status:** Approved (design), pending implementation plan

## Problem

Users want recurring automated work — "pull all the GitLab comments every
morning at 8 AM", "remind me about standup on weekdays" — created from
simple natural-language instructions, without hand-writing cron syntax or
ticket bodies. Today AIForgeCrew has no user-facing scheduler: the only
recurring machinery is cron-friendly CLI maintenance entry points
(`cli/maintenance.py`) wired to OS cron, invisible to the UI.

## Decisions (from brainstorming)

- **Execution model:** a fired job **creates a ticket** that runs through
  the existing agent pipeline (full tool surface, worktree isolation,
  Kanban/trace visibility) — not a chat turn, not a bespoke executor.
- **Caution requirement:** **preview before save.** NL instructions are
  parsed into a draft (schedule + ticket template) and shown to the user
  for confirmation; nothing is ever silently scheduled.
- **Scheduler runtime:** an **asyncio background task inside the existing
  `api` docker service** (ticks ~every 30 s). No new container, no deploy
  changes.
- **UI layout:** **inline create card + jobs table** on one page,
  matching the existing `Tickets.tsx` pattern (`+ New Job` opens a card
  above the table).
- **Missed runs:** **fire once if missed.** If the service was down when
  a job was due, startup fires exactly ONE catch-up run per job (three
  missed days ≠ three tickets), then recomputes the next run.
- **Parsing approach:** hybrid — one triage-tier LLM call at creation
  time parses NL into strict JSON; `croniter` validates and computes run
  times deterministically. The LLM never runs per-tick.

## Architecture

### 1. Job model + storage

New `scheduled_jobs` table following the existing dual-backend store
pattern (SQLite default, PG when `AIFORGE_PG_URL` is set — same as the
tickets store):

| column | type | notes |
|---|---|---|
| `id` | int PK | |
| `name` | text | short human label, from the parse |
| `cron` | text | standard 5-field cron expression |
| `ticket_title` | text | title of the ticket each fire creates |
| `ticket_body` | text | body of the ticket each fire creates |
| `project` | text nullable | target repo/project for the ticket |
| `enabled` | bool | default true; pause = false |
| `last_run_at` | timestamp nullable | set after each fire |
| `next_run_at` | timestamp | computed via croniter on save/fire |
| `last_error` | text nullable | last fire-time failure, cleared on success |
| `created_at` | timestamp | |

New module: `aiforge_core/jobs/store.py` (CRUD + due-query).

**Amended at plan time:** single-file SQLite following the
`runtime/chat_store.py` precedent (module `_DDL`, `_conn()` context
manager, WAL, path under `$AIFORGE_CONFIG_DIR` → the compose
`app_state` volume) — NOT the tickets store's 3-file backend-factory.
Jobs are small operator-local scheduling state, exactly like chat
sessions; the dual-backend machinery isn't worth it for one table.
Env override: `AIFORGE_JOBS_DB_PATH`. Timezone: the server's local
timezone (croniter over `datetime.now()`); no per-job timezone field in
v1 (single-operator deployments — YAGNI).

### 2. NL parsing (`aiforge_core/jobs/parse.py`)

One capped LLM call (role: `triage`, temperature 0, strict-JSON prompt —
same conventions as `rule_capture.classify()`):

```
input:  "pull all the GitLab comments every day at 8am"
output: {"name": "GitLab comments digest",
         "cron": "0 8 * * *",
         "ticket_title": "Pull GitLab comments (daily digest)",
         "ticket_body": "<clear instructions for the agent>",
         "project": null}
```

- JSON extracted with the same brace-balanced parser pattern used in
  `rule_capture._extract_json`.
- `croniter.is_valid(cron)` gates the result — an invalid cron (or any
  parse failure) returns a structured error for the preview UI; nothing
  is saved. Fail closed at creation time (unlike runtime paths, which
  fail open — a bad job should never be born).
- A human-readable schedule description ("Every day at 8:00 AM") is
  derived deterministically for the preview card (small formatter over
  the cron fields, no second LLM call).

### 3. API endpoints (`aiforge_core/api/api.py`)

| endpoint | behavior |
|---|---|
| `POST /api/jobs/preview` | body `{instructions}` → parse → `{draft, human_schedule, next_runs[3]}` or `{error}`; **saves nothing** |
| `POST /api/jobs` | body = confirmed/edited draft → validate cron again → insert, compute `next_run_at` |
| `GET /api/jobs` | list with status fields |
| `PATCH /api/jobs/{id}` | edit any draft field / toggle `enabled` |
| `DELETE /api/jobs/{id}` | remove |
| `POST /api/jobs/{id}/run-now` | manual fire (same code path as the scheduler tick); works even when the job is paused; recomputes `next_run_at` from now like any fire (a no-op for a non-overdue recurring job) |

### 4. Scheduler loop (`aiforge_core/jobs/scheduler.py`)

- Started from the API service's startup hook as a **daemon thread**
  (`threading.Thread(daemon=True)` — amended at plan time: this is the
  codebase's universal background-work pattern, api.py already runs 4
  such workers and zero asyncio-from-startup tasks); ticks every 30 s
  (`AIFORGE_JOBS_TICK_S`, default 30).
- Each tick: `store.due_jobs(now)` → for each, **fire**:
  1. create a ticket via the existing tickets store —
     `title=ticket_title, body=ticket_body, project=project,
     metadata={"source": "scheduled_job", "job_id": id}`;
  2. set `last_run_at=now`, clear `last_error`, recompute
     `next_run_at` via croniter.
  - Fire failure (ticket store error): log **warning**, set
    `last_error`, still advance `next_run_at` (a broken fire must not
    hot-loop every tick).
- **Catch-up:** the due-query is simply `next_run_at <= now AND
  enabled` — a job missed while the service was down is naturally "due"
  at startup and fires exactly once, because recomputing `next_run_at`
  from *now* (not from the missed slot) collapses any backlog into one
  run. No special startup pass needed; the semantics fall out of the
  query.
- Kill switch: `AIFORGE_JOBS_DISABLE=1` skips starting the loop.

### 5. UI (`web/src/views/Jobs.tsx` + nav entry)

Layout A (matches `Tickets.tsx` conventions):
- Page header: "Scheduled Jobs" + `+ New Job` button.
- New-job card (inline, above the table): one textarea for NL
  instructions + **Preview** button → renders the parsed draft as an
  editable card — human schedule ("Every day at 8:00 AM"), next 3 run
  times, ticket title/body (both editable inline) — with **Confirm &
  schedule** / **Cancel**. Parse errors render in the card, nothing
  saved.
- Table: Name · Schedule (human words) · Next run · Last run ·
  Status (Active / Paused / chip showing `last_error` when set) ·
  row actions: Run now / Pause–Resume / Delete.
- Fired tickets are ordinary tickets — visible in Kanban/Tickets as
  usual. `metadata.source = "scheduled_job"` is recorded on every fired
  ticket; a visible "scheduled" badge in the Tickets/Kanban views is
  **deferred to a follow-up** (needs the ticket-list endpoint to expose
  metadata to the UI — out of this plan's scope).

## Error handling

- **Creation time — fail closed:** parse/validation errors block the
  save and surface in the preview card.
- **Fire time — fail soft, visibly:** ticket-creation failure logs a
  warning, records `last_error` on the row (surfaced as a status chip),
  advances the schedule. Never retries in a tight loop, never crashes
  the scheduler task; one job's failure never blocks other due jobs.
- **Loop resilience:** each tick wraps per-job work in its own
  try/except; the loop itself catches everything and keeps ticking.

## Testing

- `parse.py`: valid parse happy path, invalid-cron rejection, LLM
  garbage/non-JSON rejection, brace-balanced extraction (hermetic —
  LLM client monkeypatched).
- `store.py`: CRUD, due-query semantics (due/not-due/disabled),
  `next_run_at` recompute.
- `scheduler.py`: fire creates a ticket with the right metadata,
  catch-up collapses a backlog to one run, fire-failure records
  `last_error` and still advances, disabled jobs never fire, one job's
  exception doesn't block the rest — all with an injected clock, no
  sleeping.
- API: preview-saves-nothing, save-validates-again, run-now shares the
  fire path.

## Rollout

- New dependency: `croniter` (pure-python, tiny).
- Flags: `AIFORGE_JOBS_TICK_S` (default 30), `AIFORGE_JOBS_DISABLE=1`
  (kill switch).
- No migration for existing data; the new table is created on first use
  like the other stores.

## Out of scope (v1)

- Per-job timezones (server-local only).
- Per-job catch-up policy (fixed: fire once if missed).
- One-time ("run once at 3pm tomorrow") jobs — cron-recurring only.
- Job-to-job dependencies/chaining.
- Editing the NL and re-parsing an existing job (edit the parsed fields
  directly instead).
