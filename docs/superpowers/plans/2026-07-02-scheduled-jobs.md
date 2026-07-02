# Scheduled Jobs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Recurring jobs created from natural-language instructions ("pull GitLab comments every day at 8am") that fire by creating tickets through the existing agent pipeline, with a preview-before-save gate and a simple Jobs UI page.

**Architecture:** New `aiforge_core/jobs/` package with three focused modules — `store.py` (single-file SQLite, `chat_store.py` pattern), `parse.py` (one triage-tier LLM call at creation time + deterministic croniter validation), `scheduler.py` (daemon-thread tick loop fired from the API startup hook; a due job creates a ticket via the existing tickets store). Six API endpoints in `api.py`, one new `Jobs.tsx` view wired into `main.tsx`.

**Tech Stack:** Python 3.12 (repo `.venv`), FastAPI, sqlite3, croniter (new dep), React 18 + react-router 6 + @tanstack/react-query (existing), pytest.

**Spec:** [docs/superpowers/specs/2026-07-02-scheduled-jobs-design.md](../specs/2026-07-02-scheduled-jobs-design.md)

**Test command:** always `.venv/bin/python -m pytest ...` from the repo root (or `../../../.venv/bin/python` from a `.worktrees/<branch>` worktree) — the system `python3` is 3.10 and lacks `google-adk`; the repo venv is 3.12 and correct.

**Env flags introduced:** `AIFORGE_JOBS_DB_PATH` (default `$AIFORGE_CONFIG_DIR/jobs.db`), `AIFORGE_JOBS_TICK_S` (default 30), `AIFORGE_JOBS_DISABLE=1` (kill switch).

---

### Task 1: `jobs/store.py` — SQLite store

**Files:**
- Modify: `pyproject.toml` (add croniter dep)
- Create: `aiforge_core/jobs/__init__.py` (empty)
- Create: `aiforge_core/jobs/store.py`
- Test: `tests/python/jobs/__init__.py` (empty), `tests/python/jobs/test_jobs_store.py` (new)

- [ ] **Step 1: Add the croniter dependency**

In `pyproject.toml`, inside the `[project] dependencies = [` list (alphabetically near `"fastapi>=0.115"`), add:

```toml
    "croniter>=2.0",
```

Then run: `uv sync` (or `.venv/bin/pip install "croniter>=2.0"` if uv is unavailable) and verify: `.venv/bin/python -c "import croniter; print(croniter.__name__)"` → prints `croniter`.

- [ ] **Step 2: Write the failing tests**

Create `tests/python/jobs/__init__.py` (empty file) and `tests/python/jobs/test_jobs_store.py`:

```python
"""Jobs store — CRUD + due-query semantics against an isolated tmp DB."""
from __future__ import annotations

import pytest

from aiforge_core.jobs import store


@pytest.fixture(autouse=True)
def _tmp_db(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_JOBS_DB_PATH", str(tmp_path / "jobs.db"))


def _mk(**over):
    base = dict(name="digest", cron="0 8 * * *",
                ticket_title="Pull GitLab comments",
                ticket_body="Fetch and summarize all new GitLab comments.",
                project=None, next_run_at="2026-07-03T08:00:00")
    base.update(over)
    return store.create(**base)


def test_create_and_get_roundtrip():
    j = _mk()
    assert j["id"] > 0
    assert j["enabled"] is True
    assert j["last_run_at"] is None
    got = store.get(j["id"])
    assert got["name"] == "digest"
    assert got["cron"] == "0 8 * * *"


def test_list_jobs_returns_all():
    _mk(name="a")
    _mk(name="b")
    assert [j["name"] for j in store.list_jobs()] == ["a", "b"]


def test_update_whitelisted_fields():
    j = _mk()
    out = store.update(j["id"], name="renamed", enabled=False)
    assert out["name"] == "renamed"
    assert out["enabled"] is False


def test_update_unknown_field_rejected():
    j = _mk()
    with pytest.raises(ValueError):
        store.update(j["id"], nonsense="x")


def test_delete():
    j = _mk()
    assert store.delete(j["id"]) is True
    assert store.get(j["id"]) is None
    assert store.delete(j["id"]) is False


def test_due_jobs_semantics():
    past = _mk(name="past", next_run_at="2026-07-01T08:00:00")
    _mk(name="future", next_run_at="2099-01-01T08:00:00")
    paused = _mk(name="paused", next_run_at="2026-07-01T08:00:00")
    store.update(paused["id"], enabled=False)
    due = store.due_jobs("2026-07-02T00:00:00")
    assert [j["name"] for j in due] == ["past"]
    assert due[0]["id"] == past["id"]


def test_mark_fired_success_clears_error():
    j = _mk()
    store.update(j["id"], last_error="old boom")
    store.mark_fired(j["id"], last_run_at="2026-07-03T08:00:01",
                     next_run_at="2026-07-04T08:00:00")
    got = store.get(j["id"])
    assert got["last_run_at"] == "2026-07-03T08:00:01"
    assert got["next_run_at"] == "2026-07-04T08:00:00"
    assert got["last_error"] is None


def test_mark_fired_failure_records_error():
    j = _mk()
    store.mark_fired(j["id"], last_run_at="2026-07-03T08:00:01",
                     next_run_at="2026-07-04T08:00:00", last_error="boom")
    assert store.get(j["id"])["last_error"] == "boom"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/python/jobs/test_jobs_store.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'aiforge_core.jobs'`

- [ ] **Step 4: Implement the store**

Create `aiforge_core/jobs/__init__.py` (empty) and `aiforge_core/jobs/store.py`:

```python
"""Scheduled-jobs store — single-file SQLite, `runtime/chat_store.py`
pattern (module DDL, WAL, context-manager connection). Jobs are small
operator-local scheduling state, like chat sessions — the tickets
store's dual-backend machinery is deliberately NOT used here.

Path: $AIFORGE_JOBS_DB_PATH, default $AIFORGE_CONFIG_DIR/jobs.db —
under the compose ``app_state`` volume so jobs survive redeploys.

Timestamps are ISO-8601 strings (second precision, server-local time);
lexicographic comparison == chronological, so the due-query is a plain
string compare.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime

_DDL = """
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  cron TEXT NOT NULL,
  ticket_title TEXT NOT NULL,
  ticket_body TEXT NOT NULL,
  project TEXT,
  enabled INTEGER NOT NULL DEFAULT 1,
  last_run_at TEXT,
  next_run_at TEXT NOT NULL,
  last_error TEXT,
  created_at TEXT NOT NULL
);
"""

_UPDATABLE = {"name", "cron", "ticket_title", "ticket_body", "project",
              "enabled", "next_run_at", "last_error"}


def _db_path() -> str:
    raw = os.environ.get("AIFORGE_JOBS_DB_PATH")
    if raw:
        return os.path.expanduser(raw)
    cfg = os.environ.get("AIFORGE_CONFIG_DIR", "~/.aiforge")
    return os.path.join(os.path.expanduser(cfg), "jobs.db")


@contextmanager
def _conn():
    path = _db_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(_DDL)
        yield con
        con.commit()
    finally:
        con.close()


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["enabled"] = bool(d["enabled"])
    return d


def create(*, name: str, cron: str, ticket_title: str, ticket_body: str,
           project: str | None = None, next_run_at: str) -> dict:
    with _conn() as con:
        cur = con.execute(
            "INSERT INTO jobs (name, cron, ticket_title, ticket_body, "
            "project, next_run_at, created_at) VALUES (?,?,?,?,?,?,?)",
            (name, cron, ticket_title, ticket_body, project,
             next_run_at, now_iso()))
        r = con.execute("SELECT * FROM jobs WHERE id=?",
                        (cur.lastrowid,)).fetchone()
        return _row(r)


def get(job_id: int) -> dict | None:
    with _conn() as con:
        r = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return _row(r) if r else None


def list_jobs() -> list[dict]:
    with _conn() as con:
        rs = con.execute("SELECT * FROM jobs ORDER BY id").fetchall()
        return [_row(r) for r in rs]


def update(job_id: int, **fields) -> dict | None:
    bad = set(fields) - _UPDATABLE
    if bad:
        raise ValueError(f"unknown job fields: {sorted(bad)}")
    if not fields:
        return get(job_id)
    sets = ", ".join(f"{k}=?" for k in fields)
    vals = [int(v) if isinstance(v, bool) else v for v in fields.values()]
    with _conn() as con:
        con.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*vals, job_id))
        r = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return _row(r) if r else None


def delete(job_id: int) -> bool:
    with _conn() as con:
        cur = con.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        return cur.rowcount > 0


def due_jobs(now: str) -> list[dict]:
    """Enabled jobs whose next_run_at has passed. A job missed while the
    service was down is naturally 'due' at startup — catch-up-once falls
    out of this query plus mark_fired recomputing from *now*."""
    with _conn() as con:
        rs = con.execute(
            "SELECT * FROM jobs WHERE enabled=1 AND next_run_at<=? "
            "ORDER BY id", (now,)).fetchall()
        return [_row(r) for r in rs]


def mark_fired(job_id: int, *, last_run_at: str, next_run_at: str,
               last_error: str | None = None) -> None:
    with _conn() as con:
        con.execute(
            "UPDATE jobs SET last_run_at=?, next_run_at=?, last_error=? "
            "WHERE id=?", (last_run_at, next_run_at, last_error, job_id))
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/python/jobs/test_jobs_store.py -v`
Expected: PASS (9 passed)

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock aiforge_core/jobs/ tests/python/jobs/
git commit -m "feat: scheduled-jobs SQLite store + croniter dep"
```

(If `uv sync` updated `uv.lock`, include it; if not present, skip it.)

---

### Task 2: `jobs/parse.py` — NL → draft (LLM + croniter validation)

**Files:**
- Create: `aiforge_core/jobs/parse.py`
- Test: `tests/python/jobs/test_jobs_parse.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/python/jobs/test_jobs_parse.py`:

```python
"""NL → job-draft parsing: one hermetic LLM call, croniter-gated.
Fail CLOSED at creation time — a bad job must never be born."""
from __future__ import annotations

import json

from aiforge_core.jobs import parse


def _fake(payload):
    def _complete(role, messages, **kw):
        _fake.role = role
        return payload
    return _complete


def test_parse_happy_path(monkeypatch):
    monkeypatch.setattr("aiforge_core.llm.client.complete", _fake(json.dumps({
        "name": "GitLab comments digest", "cron": "0 8 * * *",
        "ticket_title": "Pull GitLab comments (daily digest)",
        "ticket_body": "Fetch and summarize all new GitLab comments.",
        "project": None})))
    out = parse.parse_instructions("pull all gitlab comments every day at 8am")
    assert out["ok"] is True
    assert out["draft"]["cron"] == "0 8 * * *"
    assert out["human_schedule"] == "Every day at 08:00"
    assert len(out["next_runs"]) == 3
    assert _fake.role == "triage"


def test_parse_invalid_cron_fails_closed(monkeypatch):
    monkeypatch.setattr("aiforge_core.llm.client.complete", _fake(json.dumps({
        "name": "x", "cron": "99 99 * * *",
        "ticket_title": "t", "ticket_body": "b", "project": None})))
    out = parse.parse_instructions("do something weird")
    assert out["ok"] is False
    assert "cron" in out["error"].lower()


def test_parse_non_json_fails_closed(monkeypatch):
    monkeypatch.setattr("aiforge_core.llm.client.complete",
                        _fake("sorry, I can't do that"))
    out = parse.parse_instructions("anything")
    assert out["ok"] is False


def test_parse_missing_fields_fail_closed(monkeypatch):
    monkeypatch.setattr("aiforge_core.llm.client.complete", _fake(json.dumps({
        "name": "x", "cron": "0 8 * * *"})))   # no ticket_title/body
    out = parse.parse_instructions("anything")
    assert out["ok"] is False


def test_parse_llm_error_fails_closed(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("llm down")
    monkeypatch.setattr("aiforge_core.llm.client.complete", boom)
    out = parse.parse_instructions("anything")
    assert out["ok"] is False


def test_human_schedule_common_shapes():
    assert parse.human_schedule("0 8 * * *") == "Every day at 08:00"
    assert parse.human_schedule("45 9 * * 1-5") == "Weekdays at 09:45"
    assert parse.human_schedule("30 17 * * 5") == "Every Friday at 17:30"
    assert parse.human_schedule("*/15 * * * *") == "Every 15 minutes"
    # Anything unusual falls back to the raw expression.
    assert parse.human_schedule("0 8 1 * *") == "cron: 0 8 1 * *"


def test_next_runs_deterministic():
    from datetime import datetime
    runs = parse.next_runs("0 8 * * *", n=2,
                           base=datetime(2026, 7, 2, 12, 0, 0))
    assert runs == ["2026-07-03T08:00:00", "2026-07-04T08:00:00"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/python/jobs/test_jobs_parse.py -v`
Expected: FAIL with `ModuleNotFoundError`/`AttributeError` (parse module missing)

- [ ] **Step 3: Implement parse.py**

Create `aiforge_core/jobs/parse.py`:

```python
"""NL instructions → job draft. ONE capped triage-tier LLM call at
creation time (same conventions as rule_capture.classify: strict JSON,
temperature 0, brace-balanced extraction), then deterministic croniter
validation. Fails CLOSED — unlike runtime paths, a parse/validation
error here blocks the save; a bad job must never be born. The LLM never
runs per-tick; scheduling is pure cron math."""
from __future__ import annotations

import logging
import os
from datetime import datetime

from croniter import croniter

# Reuse the battle-tested brace-balanced JSON extractor (string-aware);
# duplicating 30 lines of parser is worse than this private import.
from aiforge_core.runtime.rule_capture import _extract_json

log = logging.getLogger("aiforge.jobs")

_REQUIRED = ("name", "cron", "ticket_title", "ticket_body")

_SYS = (
    "You turn a user's natural-language request for a RECURRING job into "
    "strict JSON. The job fires on a cron schedule and each fire creates "
    "a ticket for an autonomous coding agent to execute.\n\n"
    "Rules:\n"
    "- \"cron\": a standard 5-field cron expression for the schedule the "
    "user described. Times are the server's local time.\n"
    "- \"name\": a short human label (3-6 words).\n"
    "- \"ticket_title\": a one-line imperative title for the ticket.\n"
    "- \"ticket_body\": clear, self-contained instructions the agent can "
    "act on without this conversation's context.\n"
    "- \"project\": the target repo/project name if the user named one, "
    "else null.\n\n"
    "Respond with STRICT JSON ONLY, no prose, no code fence:\n"
    '{"name":"...","cron":"m h dom mon dow","ticket_title":"...",'
    '"ticket_body":"...","project":null}'
)

_DAYS = {"0": "Sunday", "1": "Monday", "2": "Tuesday", "3": "Wednesday",
         "4": "Thursday", "5": "Friday", "6": "Saturday", "7": "Sunday"}


def _timeout_s() -> int:
    try:
        return max(1, int(os.environ.get("AIFORGE_JOBS_PARSE_TIMEOUT_S", "60")))
    except (TypeError, ValueError):
        return 60


def human_schedule(cron: str) -> str:
    """Plain-words description for the common shapes; raw cron fallback."""
    parts = (cron or "").split()
    if len(parts) != 5:
        return f"cron: {cron}"
    m, h, dom, mon, dow = parts
    if m.startswith("*/") and h == "*" and (dom, mon, dow) == ("*", "*", "*"):
        return f"Every {m[2:]} minutes"
    if m.isdigit() and h.isdigit() and (dom, mon) == ("*", "*"):
        hhmm = f"{int(h):02d}:{int(m):02d}"
        if dow == "*":
            return f"Every day at {hhmm}"
        if dow == "1-5":
            return f"Weekdays at {hhmm}"
        if dow in _DAYS:
            return f"Every {_DAYS[dow]} at {hhmm}"
    return f"cron: {cron}"


def next_runs(cron: str, n: int = 3, base: datetime | None = None) -> list[str]:
    it = croniter(cron, base or datetime.now())
    return [it.get_next(datetime).isoformat(timespec="seconds")
            for _ in range(n)]


def parse_instructions(instructions: str) -> dict:
    """→ {"ok": True, "draft": {...}, "human_schedule": str,
    "next_runs": [iso, iso, iso]} or {"ok": False, "error": str}."""
    text = (instructions or "").strip()
    if not text:
        return {"ok": False, "error": "empty instructions"}
    try:
        from aiforge_core.llm import client
        raw = client.complete("triage", [
            {"role": "system", "content": _SYS},
            {"role": "user", "content": text[:4000]},
        ], temperature=0.0, max_tokens=600, timeout_s=_timeout_s())
    except Exception as exc:  # noqa: BLE001 — fail closed with a message
        log.warning("jobs.parse llm error: %s", exc)
        return {"ok": False, "error": f"parser unavailable: {exc}"}
    obj = _extract_json(raw or "")
    if not isinstance(obj, dict):
        return {"ok": False, "error": "could not parse the instructions — "
                                      "try rephrasing (e.g. 'every day at 8am, "
                                      "pull the GitLab comments')"}
    missing = [k for k in _REQUIRED if not str(obj.get(k) or "").strip()]
    if missing:
        return {"ok": False, "error": f"parse incomplete — missing {missing}"}
    cron = str(obj["cron"]).strip()
    if not croniter.is_valid(cron):
        return {"ok": False,
                "error": f"invalid cron expression from parse: {cron!r}"}
    project = obj.get("project")
    draft = {
        "name": str(obj["name"]).strip()[:120],
        "cron": cron,
        "ticket_title": str(obj["ticket_title"]).strip()[:200],
        "ticket_body": str(obj["ticket_body"]).strip()[:4000],
        "project": (str(project).strip() or None) if project else None,
    }
    return {"ok": True, "draft": draft,
            "human_schedule": human_schedule(cron),
            "next_runs": next_runs(cron)}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/python/jobs/test_jobs_parse.py -v`
Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/jobs/parse.py tests/python/jobs/test_jobs_parse.py
git commit -m "feat: NL-to-job-draft parser (triage LLM + croniter gate)"
```

---

### Task 3: `jobs/scheduler.py` — fire + tick loop

**Files:**
- Create: `aiforge_core/jobs/scheduler.py`
- Test: `tests/python/jobs/test_jobs_scheduler.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/python/jobs/test_jobs_scheduler.py`:

```python
"""Scheduler fire/tick with an injected clock. No sleeping, no threads —
run_loop is a trivial wrapper over tick() and is not tested here."""
from __future__ import annotations

from datetime import datetime

import pytest

from aiforge_core.jobs import scheduler, store

NOW = datetime(2026, 7, 2, 12, 0, 0)


@pytest.fixture(autouse=True)
def _tmp_db(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_JOBS_DB_PATH", str(tmp_path / "jobs.db"))


@pytest.fixture()
def created(monkeypatch):
    calls: list[dict] = []

    class _T:
        id = 42
        identifier = "T-42"

    def fake_create(**kw):
        calls.append(kw)
        return _T()

    monkeypatch.setattr("aiforge_core.tickets.store.create", fake_create)
    return calls


def _mk(**over):
    base = dict(name="digest", cron="0 8 * * *",
                ticket_title="Pull GitLab comments",
                ticket_body="Fetch and summarize.", project="demo",
                next_run_at="2026-07-02T08:00:00")   # overdue vs NOW
    base.update(over)
    return store.create(**base)


def test_fire_creates_ticket_with_metadata(created):
    j = _mk()
    scheduler.fire(j, now=NOW)
    assert len(created) == 1
    kw = created[0]
    assert kw["title"] == "Pull GitLab comments"
    assert kw["project"] == "demo"
    assert kw["metadata"] == {"source": "scheduled_job", "job_id": j["id"]}
    got = store.get(j["id"])
    assert got["last_run_at"] == "2026-07-02T12:00:00"
    assert got["next_run_at"] == "2026-07-03T08:00:00"   # from NOW, not slot
    assert got["last_error"] is None


def test_fire_failure_records_error_and_still_advances(monkeypatch):
    def boom(**kw):
        raise RuntimeError("store down")
    monkeypatch.setattr("aiforge_core.tickets.store.create", boom)
    j = _mk()
    scheduler.fire(j, now=NOW)
    got = store.get(j["id"])
    assert "store down" in got["last_error"]
    assert got["next_run_at"] == "2026-07-03T08:00:00"   # advanced — no hot loop


def test_tick_fires_due_skips_future_and_disabled(created):
    _mk(name="due")
    _mk(name="future", next_run_at="2099-01-01T00:00:00")
    paused = _mk(name="paused")
    store.update(paused["id"], enabled=False)
    fired = scheduler.tick(now=NOW)
    assert fired == 1
    assert len(created) == 1


def test_tick_backlog_collapses_to_one_run(created):
    # 3 missed days — next_run_at far in the past. ONE fire, recomputed
    # from now: catch-up-once semantics.
    j = _mk(next_run_at="2026-06-29T08:00:00")
    assert scheduler.tick(now=NOW) == 1
    assert store.get(j["id"])["next_run_at"] == "2026-07-03T08:00:00"
    assert scheduler.tick(now=NOW) == 0   # nothing left due


def test_tick_one_bad_job_never_blocks_others(monkeypatch):
    calls = []

    class _T:
        id = 1
        identifier = "T-1"

    def flaky(**kw):
        calls.append(kw)
        if kw["title"] == "bad":
            raise RuntimeError("boom")
        return _T()

    monkeypatch.setattr("aiforge_core.tickets.store.create", flaky)
    _mk(name="bad", ticket_title="bad")
    good = _mk(name="good", ticket_title="good")
    fired = scheduler.tick(now=NOW)
    assert fired == 1                       # only the good one counts
    assert len(calls) == 2                  # both were attempted
    assert store.get(good["id"])["last_error"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/python/jobs/test_jobs_scheduler.py -v`
Expected: FAIL with `ImportError`/`AttributeError` (scheduler module missing)

- [ ] **Step 3: Implement scheduler.py**

Create `aiforge_core/jobs/scheduler.py`:

```python
"""Tick loop: fire due jobs by creating tickets through the existing
pipeline. Runs as a daemon thread from the API startup hook (the
codebase's universal background-work pattern). Catch-up-once semantics
fall out of the due-query + recomputing next_run_at from *now* (not the
missed slot) — a 3-day backlog collapses to one fire.

Kill switch: AIFORGE_JOBS_DISABLE=1. Tick: AIFORGE_JOBS_TICK_S (30)."""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime

from croniter import croniter

from aiforge_core.jobs import store

log = logging.getLogger("aiforge.jobs")


def _tick_s() -> int:
    try:
        return max(5, int(os.environ.get("AIFORGE_JOBS_TICK_S", "30")))
    except (TypeError, ValueError):
        return 30


def _disabled() -> bool:
    return os.environ.get("AIFORGE_JOBS_DISABLE", "").strip().lower() \
        in ("1", "true")


def fire(job: dict, *, now: datetime | None = None) -> bool:
    """Create the job's ticket and advance its schedule. Fire failure is
    soft-but-visible: last_error recorded on the row (UI chip), schedule
    STILL advances so a broken fire can't hot-loop every tick. Returns
    True on a successful fire."""
    now = now or datetime.now()
    now_s = now.isoformat(timespec="seconds")
    nxt = croniter(job["cron"], now).get_next(datetime) \
        .isoformat(timespec="seconds")
    try:
        from aiforge_core.tickets import store as tickets_mod
        t = tickets_mod.create(
            title=job["ticket_title"], body=job["ticket_body"],
            project=job.get("project"),
            metadata={"source": "scheduled_job", "job_id": job["id"]})
        store.mark_fired(job["id"], last_run_at=now_s, next_run_at=nxt)
        log.info("jobs.fired job=%s ticket=%s", job["id"],
                 getattr(t, "identifier", getattr(t, "id", "?")))
        return True
    except Exception as exc:  # noqa: BLE001 — record + advance, never raise
        store.mark_fired(job["id"], last_run_at=now_s, next_run_at=nxt,
                         last_error=str(exc)[:500])
        log.warning("jobs.fire_failed job=%s: %s", job["id"], exc)
        return False


def tick(now: datetime | None = None) -> int:
    """Fire everything due. One job's failure never blocks the rest.
    Returns the number of SUCCESSFUL fires."""
    now = now or datetime.now()
    fired = 0
    for job in store.due_jobs(now.isoformat(timespec="seconds")):
        try:
            if fire(job, now=now):
                fired += 1
        except Exception as exc:  # noqa: BLE001 — belt over fire()'s braces
            log.warning("jobs.tick job=%s crashed: %s", job.get("id"), exc)
    return fired


def run_loop() -> None:
    """Blocking loop for the daemon thread. Never raises."""
    log.info("jobs.scheduler loop started (tick=%ss)", _tick_s())
    while True:
        try:
            tick()
        except Exception as exc:  # noqa: BLE001 — the loop must survive
            log.warning("jobs.tick crashed: %s", exc)
        time.sleep(_tick_s())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/python/jobs/test_jobs_scheduler.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/jobs/scheduler.py tests/python/jobs/test_jobs_scheduler.py
git commit -m "feat: jobs scheduler — fire/tick with catch-up-once semantics"
```

---

### Task 4: API endpoints + startup wiring

**Files:**
- Modify: `aiforge_core/api/api.py` (6 endpoints + 1 startup hook; find insertion points via grep, the file is large)
- Test: `tests/api/test_jobs_api.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/api/test_jobs_api.py` (fixture mirrors `tests/api/test_rule_capture_api.py:11-33`):

```python
"""Jobs API — preview saves nothing; save re-validates; run-now shares
the fire path and works while paused."""
from __future__ import annotations

import importlib
import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app_client(monkeypatch, tmp_path):
    monkeypatch.delenv("AIFORGE_PG_URL", raising=False)
    monkeypatch.delenv("AIFORGE_FORCE_PG", raising=False)
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_DB_PATH", str(tmp_path / "api.db"))
    monkeypatch.setenv("AIFORGE_JOBS_DB_PATH", str(tmp_path / "jobs.db"))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("AIFORGE_JOBS_DISABLE", "1")   # no live loop in tests
    import aiforge_core.config.env as envmod
    importlib.reload(envmod)
    import aiforge_core.tickets.backend_factory as bf
    importlib.reload(bf)
    bf.reset_backend_for_tests()
    import aiforge_core.tickets.store as store
    importlib.reload(store)
    import aiforge_core.api.api as api
    importlib.reload(api)
    return TestClient(api.app), api


_DRAFT = {"name": "digest", "cron": "0 8 * * *",
          "ticket_title": "Pull GitLab comments",
          "ticket_body": "Fetch and summarize.", "project": None}


def test_preview_saves_nothing(app_client, monkeypatch):
    client, _ = app_client
    monkeypatch.setattr("aiforge_core.llm.client.complete",
                        lambda *a, **k: json.dumps(_DRAFT))
    r = client.post("/api/jobs/preview",
                    json={"instructions": "gitlab comments daily 8am"})
    assert r.status_code == 200
    out = r.json()
    assert out["ok"] is True
    assert out["draft"]["cron"] == "0 8 * * *"
    assert out["human_schedule"] == "Every day at 08:00"
    assert client.get("/api/jobs").json() == []          # nothing saved


def test_preview_parse_error_is_friendly_not_500(app_client, monkeypatch):
    client, _ = app_client
    monkeypatch.setattr("aiforge_core.llm.client.complete",
                        lambda *a, **k: "no json here")
    r = client.post("/api/jobs/preview", json={"instructions": "x y z"})
    assert r.status_code == 200
    assert r.json()["ok"] is False


def test_create_list_patch_delete_roundtrip(app_client):
    client, _ = app_client
    r = client.post("/api/jobs", json=_DRAFT)
    assert r.status_code == 201
    jid = r.json()["id"]
    assert r.json()["next_run_at"]                       # computed on save
    jobs = client.get("/api/jobs").json()
    assert [j["id"] for j in jobs] == [jid]
    r = client.patch(f"/api/jobs/{jid}", json={"enabled": False})
    assert r.json()["enabled"] is False
    assert client.delete(f"/api/jobs/{jid}").status_code == 200
    assert client.get("/api/jobs").json() == []


def test_create_rejects_bad_cron(app_client):
    client, _ = app_client
    r = client.post("/api/jobs", json={**_DRAFT, "cron": "not a cron"})
    assert r.status_code == 400


def test_patch_cron_revalidates_and_recomputes(app_client):
    client, _ = app_client
    jid = client.post("/api/jobs", json=_DRAFT).json()["id"]
    before = client.get("/api/jobs").json()[0]["next_run_at"]
    r = client.patch(f"/api/jobs/{jid}", json={"cron": "bad"})
    assert r.status_code == 400
    r = client.patch(f"/api/jobs/{jid}", json={"cron": "0 9 * * *"})
    assert r.status_code == 200
    assert r.json()["next_run_at"] != before


def test_run_now_fires_even_when_paused(app_client, monkeypatch):
    client, _ = app_client
    created = []

    class _T:
        id = 7
        identifier = "T-7"

    monkeypatch.setattr("aiforge_core.tickets.store.create",
                        lambda **kw: created.append(kw) or _T())
    jid = client.post("/api/jobs", json=_DRAFT).json()["id"]
    client.patch(f"/api/jobs/{jid}", json={"enabled": False})
    r = client.post(f"/api/jobs/{jid}/run-now")
    assert r.status_code == 200
    assert len(created) == 1
    assert created[0]["metadata"]["source"] == "scheduled_job"


def test_missing_job_404s(app_client):
    client, _ = app_client
    assert client.patch("/api/jobs/999", json={}).status_code == 404
    assert client.delete("/api/jobs/999").status_code == 404
    assert client.post("/api/jobs/999/run-now").status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/api/test_jobs_api.py -v`
Expected: FAIL — 404s on `/api/jobs/*` routes (endpoints don't exist)

- [ ] **Step 3: Implement endpoints in api.py**

Find the existing pydantic models section (grep `class TicketCreate(BaseModel)`, ~line 325) and add nearby:

```python
class JobPreviewBody(BaseModel):
    instructions: str = Field(..., min_length=1)


class JobCreate(BaseModel):
    name: str = Field(..., min_length=1)
    cron: str = Field(..., min_length=9)
    ticket_title: str = Field(..., min_length=1)
    ticket_body: str = Field(..., min_length=1)
    project: str | None = None


class JobPatch(BaseModel):
    name: str | None = None
    cron: str | None = None
    ticket_title: str | None = None
    ticket_body: str | None = None
    project: str | None = None
    enabled: bool | None = None
```

Find the existing tickets endpoints (grep `@app.post("/api/tickets"`, ~line 551) and add the jobs endpoints after that block:

```python
# ─────────────────────────── scheduled jobs ─────────────────────────


@app.post("/api/jobs/preview")
def jobs_preview(payload: JobPreviewBody) -> dict:
    """NL instructions → parsed draft + human schedule + next runs.
    Saves NOTHING. Parse errors come back as {ok: False, error} so the
    UI renders them in the preview card instead of a 500."""
    from aiforge_core.jobs import parse as jobs_parse
    return jobs_parse.parse_instructions(payload.instructions)


@app.post("/api/jobs", status_code=201)
def jobs_create(payload: JobCreate) -> dict:
    from croniter import croniter as _cron
    from aiforge_core.jobs import parse as jobs_parse, store as jobs_store
    if not _cron.is_valid(payload.cron):
        raise HTTPException(400, f"invalid cron: {payload.cron!r}")
    nxt = jobs_parse.next_runs(payload.cron, n=1)[0]
    return jobs_store.create(
        name=payload.name, cron=payload.cron,
        ticket_title=payload.ticket_title, ticket_body=payload.ticket_body,
        project=payload.project, next_run_at=nxt)


@app.get("/api/jobs")
def jobs_list() -> list[dict]:
    from aiforge_core.jobs import parse as jobs_parse, store as jobs_store
    out = jobs_store.list_jobs()
    for j in out:
        j["human_schedule"] = jobs_parse.human_schedule(j["cron"])
    return out


@app.patch("/api/jobs/{job_id}")
def jobs_patch(job_id: int, payload: JobPatch) -> dict:
    from croniter import croniter as _cron
    from aiforge_core.jobs import parse as jobs_parse, store as jobs_store
    if jobs_store.get(job_id) is None:
        raise HTTPException(404, f"job {job_id} not found")
    fields = {k: v for k, v in payload.model_dump().items() if v is not None}
    if "cron" in fields:
        if not _cron.is_valid(fields["cron"]):
            raise HTTPException(400, f"invalid cron: {fields['cron']!r}")
        fields["next_run_at"] = jobs_parse.next_runs(fields["cron"], n=1)[0]
    return jobs_store.update(job_id, **fields)


@app.delete("/api/jobs/{job_id}")
def jobs_delete(job_id: int) -> dict:
    from aiforge_core.jobs import store as jobs_store
    if not jobs_store.delete(job_id):
        raise HTTPException(404, f"job {job_id} not found")
    return {"ok": True}


@app.post("/api/jobs/{job_id}/run-now")
def jobs_run_now(job_id: int) -> dict:
    """Manual fire — same code path as the scheduler tick; works even
    when the job is paused."""
    from aiforge_core.jobs import scheduler as jobs_scheduler
    from aiforge_core.jobs import store as jobs_store
    job = jobs_store.get(job_id)
    if job is None:
        raise HTTPException(404, f"job {job_id} not found")
    ok = jobs_scheduler.fire(job)
    return {"ok": ok, "job": jobs_store.get(job_id)}
```

Find the existing startup hooks (grep `@app.on_event("startup")`, ~lines 65-96) and add a third, following the same defensive shape:

```python
@app.on_event("startup")
def _start_jobs_scheduler() -> None:
    """Scheduled-jobs tick loop — daemon thread, same pattern as the
    other background workers. AIFORGE_JOBS_DISABLE=1 skips it."""
    try:
        import threading

        from aiforge_core.jobs import scheduler as jobs_scheduler
        if jobs_scheduler._disabled():
            return
        threading.Thread(target=jobs_scheduler.run_loop,
                         daemon=True, name="jobs-scheduler").start()
    except Exception:  # noqa: BLE001 — startup must never crash the API
        pass
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/api/test_jobs_api.py -v`
Expected: PASS (7 passed)

- [ ] **Step 5: Run neighboring api tests for regression**

Run: `.venv/bin/python -m pytest tests/api/ -q`
Expected: no NEW failures (the known pre-existing `test_prefilter_skips_classify_for_trivial` full-suite flake may appear — verify it also fails without your change before blaming it, or run it alone where it passes)

- [ ] **Step 6: Commit**

```bash
git add aiforge_core/api/api.py tests/api/test_jobs_api.py
git commit -m "feat: jobs API endpoints + scheduler startup wiring"
```

---

### Task 5: Frontend — Jobs view + nav

**Files:**
- Modify: `web/src/api.ts` (add jobs methods)
- Create: `web/src/views/Jobs.tsx`
- Modify: `web/src/main.tsx` (lazy import + NAV entry + TITLE_MAP + Route)

No unit-test infra exists for web views in this repo — verification is `npm run build` (tsc + vite) plus the API contract already tested in Task 4.

- [ ] **Step 1: Add api.ts methods**

In `web/src/api.ts`, add types near the other interfaces and methods inside the `export const api = {...}` object (match existing style exactly — see `addModel` for the POST shape):

```ts
export interface JobDraft {
  name: string; cron: string; ticket_title: string;
  ticket_body: string; project: string | null;
}
export interface JobPreview {
  ok: boolean; error?: string; draft?: JobDraft;
  human_schedule?: string; next_runs?: string[];
}
export interface Job extends JobDraft {
  id: number; enabled: boolean; last_run_at: string | null;
  next_run_at: string; last_error: string | null;
  created_at: string; human_schedule: string;
}
```

```ts
  previewJob: (instructions: string) =>
    j<JobPreview>('/jobs/preview', { method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ instructions }) }),
  createJob: (draft: JobDraft) =>
    j<Job>('/jobs', { method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(draft) }),
  listJobs: () => j<Job[]>('/jobs'),
  patchJob: (id: number, patch: Partial<JobDraft & { enabled: boolean }>) =>
    j<Job>(`/jobs/${id}`, { method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch) }),
  deleteJob: (id: number) =>
    j<{ ok: boolean }>(`/jobs/${id}`, { method: 'DELETE' }),
  runJobNow: (id: number) =>
    j<{ ok: boolean; job: Job }>(`/jobs/${id}/run-now`, { method: 'POST' }),
```

- [ ] **Step 2: Create Jobs.tsx**

Create `web/src/views/Jobs.tsx` (Layout A — inline create card above the table, matching `Tickets.tsx` conventions: `page-header`, `card`, `stack`, `field`, `chip`, sonner toasts, react-query):

```tsx
import { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { api, Job, JobDraft, JobPreview } from '../api';

export default function Jobs() {
  const qc = useQueryClient();
  const { data: jobs, isLoading } = useQuery({
    queryKey: ['jobs'], queryFn: api.listJobs, refetchInterval: 30_000,
  });
  const [creating, setCreating] = useState(false);
  const [instructions, setInstructions] = useState('');
  const [preview, setPreview] = useState<JobPreview | null>(null);
  const [draft, setDraft] = useState<JobDraft | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = () => qc.invalidateQueries({ queryKey: ['jobs'] });

  const doPreview = async () => {
    setBusy(true);
    try {
      const p = await api.previewJob(instructions);
      setPreview(p);
      setDraft(p.ok && p.draft ? { ...p.draft } : null);
    } catch (e: any) {
      toast.error(e?.message || 'preview failed');
    } finally { setBusy(false); }
  };

  const doConfirm = async () => {
    if (!draft) return;
    setBusy(true);
    try {
      await api.createJob(draft);
      toast.success('Job scheduled');
      setCreating(false); setInstructions(''); setPreview(null); setDraft(null);
      refresh();
    } catch (e: any) {
      toast.error(e?.message || 'save failed');
    } finally { setBusy(false); }
  };

  const doRunNow = async (id: number) => {
    try {
      const r = await api.runJobNow(id);
      r.ok ? toast.success('Fired — ticket created')
           : toast.error('Fire failed — see job status');
      refresh();
    } catch (e: any) { toast.error(e?.message || 'run failed'); }
  };

  const doToggle = async (jb: Job) => {
    try { await api.patchJob(jb.id, { enabled: !jb.enabled }); refresh(); }
    catch (e: any) { toast.error(e?.message || 'update failed'); }
  };

  const doDelete = async (id: number) => {
    if (!window.confirm('Delete this job? Its past tickets are kept.')) return;
    try { await api.deleteJob(id); refresh(); }
    catch (e: any) { toast.error(e?.message || 'delete failed'); }
  };

  return (
    <div>
      <div className="page-header">
        <h1>Scheduled Jobs</h1>
        <button onClick={() => setCreating(c => !c)}>
          {creating ? 'Cancel' : '+ New Job'}
        </button>
      </div>

      {creating && (
        <div className="card" style={{ marginBottom: 16 }}>
          <div className="card-header"><h2>New job</h2></div>
          <div className="stack">
            <label className="field">Describe the job in plain words
              <textarea rows={3} autoFocus value={instructions}
                placeholder="e.g. pull all the GitLab comments every day at 8am"
                onChange={e => setInstructions(e.target.value)} />
            </label>
            <div>
              <button disabled={busy || !instructions.trim()}
                onClick={doPreview}>
                {busy ? 'Parsing…' : 'Preview'}
              </button>
            </div>
            {preview && !preview.ok && (
              <div className="empty" style={{ color: 'var(--danger, #d33)' }}>
                {preview.error}
              </div>
            )}
            {preview?.ok && draft && (
              <div className="card" style={{ padding: 12 }}>
                <p><b>{preview.human_schedule}</b> — next runs:{' '}
                  {(preview.next_runs || []).map(t =>
                    new Date(t).toLocaleString()).join(' · ')}</p>
                <label className="field">Name
                  <input value={draft.name}
                    onChange={e => setDraft({ ...draft, name: e.target.value })} />
                </label>
                <label className="field">Ticket title
                  <input value={draft.ticket_title}
                    onChange={e => setDraft({ ...draft, ticket_title: e.target.value })} />
                </label>
                <label className="field">Ticket body (what the agent will do)
                  <textarea rows={4} value={draft.ticket_body}
                    onChange={e => setDraft({ ...draft, ticket_body: e.target.value })} />
                </label>
                <label className="field">Cron
                  <input value={draft.cron}
                    onChange={e => setDraft({ ...draft, cron: e.target.value })} />
                </label>
                <div style={{ display: 'flex', gap: 8 }}>
                  <button disabled={busy} onClick={doConfirm}>
                    Confirm &amp; schedule
                  </button>
                  <button className="ghost"
                    onClick={() => { setPreview(null); setDraft(null); }}>
                    Discard
                  </button>
                </div>
              </div>
            )}
          </div>
        </div>
      )}

      <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
        {isLoading ? (
          <div className="empty">Loading…</div>
        ) : !jobs?.length ? (
          <div className="empty">No scheduled jobs yet — create one above.</div>
        ) : (
          <table>
            <thead><tr>
              <th>Name</th><th>Schedule</th><th>Next run</th>
              <th>Last run</th><th>Status</th><th></th>
            </tr></thead>
            <tbody>
              {jobs.map(jb => (
                <tr key={jb.id}>
                  <td>{jb.name}</td>
                  <td>{jb.human_schedule}</td>
                  <td>{new Date(jb.next_run_at).toLocaleString()}</td>
                  <td>{jb.last_run_at
                    ? new Date(jb.last_run_at).toLocaleString() : '—'}</td>
                  <td>
                    {jb.last_error
                      ? <span className="chip danger" title={jb.last_error}>error</span>
                      : <span className={`chip ${jb.enabled ? 'ok' : ''}`}>
                          {jb.enabled ? 'Active' : 'Paused'}
                        </span>}
                  </td>
                  <td style={{ whiteSpace: 'nowrap' }}>
                    <button className="ghost" onClick={() => doRunNow(jb.id)}>
                      Run now
                    </button>
                    <button className="ghost" onClick={() => doToggle(jb)}>
                      {jb.enabled ? 'Pause' : 'Resume'}
                    </button>
                    <button className="ghost" onClick={() => doDelete(jb.id)}>
                      Delete
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
```

(Chip class names: check the actual `statusClass`/chip variants used in `Tickets.tsx` and match them — `chip ok`/`chip danger` above are placeholders for whatever the real success/error chip classes are; use the codebase's.)

- [ ] **Step 3: Wire into main.tsx**

In `web/src/main.tsx`, four one-line additions matching the existing Tickets entries exactly:
- lazy import (near line 17): `const Jobs = lazy(() => import('./views/Jobs'));`
- NAV array, **Operate** group (near line 63): `{ to: '/jobs', label: 'Jobs', icon: 'Clock' }` — check the icon set used by other entries and pick an existing clock/timer icon name from it; if none exists, reuse the Tickets icon.
- TITLE_MAP (near line 93): `'/jobs': 'Scheduled Jobs',`
- Route (near line 180): `<Route path="/jobs" element={<Jobs />} />`

- [ ] **Step 4: Build to verify**

Run: `cd web && npm run build`
Expected: tsc + vite build succeed, zero type errors. Fix any type/import errors before proceeding (chip classes, icon names — align with the real exports).

- [ ] **Step 5: Commit**

```bash
git add web/src/api.ts web/src/views/Jobs.tsx web/src/main.tsx
git commit -m "feat: Scheduled Jobs UI — NL create with preview, jobs table"
```

---

### Task 6: Full suite verification + push

**Files:** none (verification only)

- [ ] **Step 1: Run all jobs tests together**

Run:
```bash
.venv/bin/python -m pytest tests/python/jobs/ tests/api/test_jobs_api.py -v
```
Expected: all PASS (9 + 7 + 5 + 7 = 28)

- [ ] **Step 2: Full project suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: no NEW failures vs the known baseline (2 pre-existing: `test_prefilter_skips_classify_for_trivial` full-suite flake, `test_repo_standards.py::...default_compile_cmd` env mismatch — both documented in this session; verify any other failure against the pre-branch baseline before treating it as yours)

- [ ] **Step 3: Web build (if not already green from Task 5)**

Run: `cd web && npm run build`
Expected: success

- [ ] **Step 4: Push**

```bash
git push -u origin <branch>
```

Then hand off to superpowers:finishing-a-development-branch for the merge decision.
