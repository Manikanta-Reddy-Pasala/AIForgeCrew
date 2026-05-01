"""Online learner — writes step_trace + episodic on every agent step.

Postgres tables (per spec §7.2): episodic_outcomes, procedural_patterns,
audit_events, step_traces.

The connection comes from the same DSN AIForgeCrew already uses
(AIFORGE_DSN). New tables; same DB.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any


@dataclass
class StepTrace:
    ticket_id: str
    agent_role: str
    step_index: int
    plan_step_id: str = ""
    input_context: dict[str, Any] | None = None
    output: dict[str, Any] | None = None
    tools_used: list[str] | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    prompt_version: str = "v1"
    status: str = "ok"


_DSN = os.environ.get(
    "AIFORGE_DSN",
    "postgresql://manikanta@127.0.0.1:5432/aiforge",
)


def _conn():
    """Return a psycopg connection. Fail-soft: returns None if DB unreachable."""
    try:
        import psycopg
        return psycopg.connect(_DSN)
    except Exception:
        return None


_DDL = """
CREATE TABLE IF NOT EXISTS aiforge_agents_step_traces (
    id              uuid PRIMARY KEY,
    ticket_id       text,
    agent_role      text,
    step_index      int,
    plan_step_id    text,
    input_context   jsonb,
    output          jsonb,
    tools_used      text[],
    tokens_in       int,
    tokens_out      int,
    prompt_version  text,
    status          text,
    created_at      timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_aas_ticket ON aiforge_agents_step_traces(ticket_id);

CREATE TABLE IF NOT EXISTS aiforge_agents_episodic (
    id              uuid PRIMARY KEY,
    ticket_id       text,
    stage           text,
    agent_role      text,
    outcome         text,
    summary         text,
    artifacts       jsonb,
    hitl_weight     int DEFAULT 1,
    created_at      timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_aae_ticket ON aiforge_agents_episodic(ticket_id);
CREATE INDEX IF NOT EXISTS idx_aae_role   ON aiforge_agents_episodic(agent_role);

CREATE TABLE IF NOT EXISTS aiforge_agents_procedural (
    id              uuid PRIMARY KEY,
    agent_role      text,
    task_class      text,
    tool_sequence   jsonb,
    preconditions   jsonb,
    success_count   int DEFAULT 0,
    failure_count   int DEFAULT 0,
    last_used_at    timestamptz DEFAULT now(),
    skill_ref       text
);
CREATE INDEX IF NOT EXISTS idx_aap_role_class
    ON aiforge_agents_procedural(agent_role, task_class);

CREATE TABLE IF NOT EXISTS aiforge_agents_audit (
    id              bigserial PRIMARY KEY,
    ticket_id       text,
    agent_role      text,
    event_type      text,
    payload         jsonb,
    duration_ms     int,
    status          text,
    trace_id        text,
    created_at      timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_aaaud_ticket ON aiforge_agents_audit(ticket_id);

-- Skills: Learner-promoted recipes that turned out to win across
-- multiple tickets. Surfaced to Planner / Doer via context bundles.
CREATE TABLE IF NOT EXISTS aiforge_agents_skills (
    id              bigserial PRIMARY KEY,
    repo            text,
    task_class      text,
    name            text,
    summary         text,
    body_md         text,
    success_count   int  DEFAULT 0,
    failure_count   int  DEFAULT 0,
    last_used       timestamptz,
    created_at      timestamptz DEFAULT now(),
    UNIQUE (repo, task_class, name)
);
CREATE INDEX IF NOT EXISTS idx_aas_repo_task
    ON aiforge_agents_skills(repo, task_class);
"""


def migrate() -> bool:
    conn = _conn()
    if conn is None:
        return False
    try:
        with conn, conn.cursor() as cur:
            cur.execute(_DDL)
        return True
    finally:
        conn.close()


def record_step_trace(t: StepTrace) -> None:
    conn = _conn()
    if conn is None:
        return
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO aiforge_agents_step_traces "
                "(id, ticket_id, agent_role, step_index, plan_step_id, "
                " input_context, output, tools_used, tokens_in, tokens_out, "
                " prompt_version, status) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    str(uuid.uuid4()), t.ticket_id, t.agent_role, t.step_index,
                    t.plan_step_id,
                    json.dumps(t.input_context or {}),
                    json.dumps(t.output or {}),
                    t.tools_used or [],
                    t.tokens_in, t.tokens_out,
                    t.prompt_version, t.status,
                ),
            )
    finally:
        conn.close()


def record_episodic(*, ticket_id: str, stage: str, agent_role: str,
                    outcome: str, summary: str,
                    artifacts: dict | None = None,
                    hitl_weight: int = 1) -> None:
    conn = _conn()
    if conn is None:
        return
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO aiforge_agents_episodic "
                "(id, ticket_id, stage, agent_role, outcome, summary, "
                " artifacts, hitl_weight) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    str(uuid.uuid4()), ticket_id, stage, agent_role, outcome,
                    summary, json.dumps(artifacts or {}), hitl_weight,
                ),
            )
    finally:
        conn.close()


def update_procedural(*, agent_role: str, task_class: str,
                      tool_sequence: list[str], success: bool) -> None:
    conn = _conn()
    if conn is None:
        return
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, success_count, failure_count "
                "FROM aiforge_agents_procedural "
                "WHERE agent_role=%s AND task_class=%s "
                "  AND tool_sequence=%s LIMIT 1",
                (agent_role, task_class, json.dumps(tool_sequence)),
            )
            row = cur.fetchone()
            if row:
                pid, sc, fc = row
                if success:
                    sc += 1
                else:
                    fc += 1
                cur.execute(
                    "UPDATE aiforge_agents_procedural "
                    "SET success_count=%s, failure_count=%s, "
                    "    last_used_at=now() WHERE id=%s",
                    (sc, fc, pid),
                )
            else:
                cur.execute(
                    "INSERT INTO aiforge_agents_procedural "
                    "(id, agent_role, task_class, tool_sequence, "
                    " success_count, failure_count) "
                    "VALUES (%s,%s,%s,%s,%s,%s)",
                    (
                        str(uuid.uuid4()), agent_role, task_class,
                        json.dumps(tool_sequence),
                        1 if success else 0,
                        0 if success else 1,
                    ),
                )
    finally:
        conn.close()


def record_audit(*, ticket_id: str, agent_role: str, event_type: str,
                 payload: dict | None, duration_ms: int = 0,
                 status: str = "ok", trace_id: str | None = None) -> bool:
    conn = _conn()
    if conn is None:
        return False
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO aiforge_agents_audit "
                "(ticket_id, agent_role, event_type, payload, duration_ms, "
                " status, trace_id) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (ticket_id, agent_role, event_type,
                 json.dumps(payload or {}),
                 duration_ms, status, trace_id),
            )
        return True
    except Exception:
        return False
    finally:
        conn.close()


# ─────────── Skills (P2) ─────────────────────────────────────────────

def promote_skill(*, repo: str, task_class: str, name: str,
                  summary: str, body_md: str, success: bool) -> bool:
    """Upsert a skill row. On conflict, increment counters + bump
    last_used. The Planner pulls these via `top_skills_for`."""
    conn = _conn()
    if conn is None:
        return False
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO aiforge_agents_skills "
                "(repo, task_class, name, summary, body_md, "
                " success_count, failure_count, last_used) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s, now()) "
                "ON CONFLICT (repo, task_class, name) DO UPDATE SET "
                "  success_count = aiforge_agents_skills.success_count + EXCLUDED.success_count, "
                "  failure_count = aiforge_agents_skills.failure_count + EXCLUDED.failure_count, "
                "  body_md = EXCLUDED.body_md, "
                "  summary = EXCLUDED.summary, "
                "  last_used = now()",
                (repo, task_class, name, summary, body_md,
                 1 if success else 0, 0 if success else 1),
            )
        return True
    except Exception:
        return False
    finally:
        conn.close()


def top_skills_for(*, repo: str, task_class: str,
                   k: int = 3) -> list[dict]:
    """Return up to k skills ordered by net success. Used by Planner /
    Doer prompts to recall winning recipes for similar tasks."""
    conn = _conn()
    if conn is None:
        return []
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "SELECT name, summary, body_md, success_count, failure_count "
                "FROM aiforge_agents_skills "
                "WHERE repo = %s AND task_class = %s "
                "ORDER BY (success_count - failure_count) DESC, last_used DESC "
                "LIMIT %s",
                (repo, task_class, k),
            )
            rows = cur.fetchall()
        return [
            {"name": r[0], "summary": r[1], "body_md": r[2],
             "success_count": r[3], "failure_count": r[4]}
            for r in rows
        ]
    except Exception:
        return []
    finally:
        conn.close()
