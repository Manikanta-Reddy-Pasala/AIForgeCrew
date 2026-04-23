#!/usr/bin/env python3
"""Ops SQL agent over the aiforge Postgres (read-only).

Shipped from EVAL-3 (2026-04-23). smolagents CodeAgent + a read-only
`sql_engine` tool; schema doc auto-derived via ``information_schema``.

Usage:
    scripts/sql_agent.py "how many tickets are cancelled today?"
    scripts/sql_agent.py --tables tickets,ticket_events,memories \\
        --model qwen3.6-35b-a3b "<question>"

Requires:
- ``aiforge_ro`` Postgres role (see db/migrations/2026-04-23-aiforge-ro-role.sql).
- LM Studio at ``$LM_BASE`` with the model loaded (``lms load <id>
  --context-length 32768 --ttl 43200`` — default TTL=1h idle-unloads mid-run).
"""
from __future__ import annotations

import argparse
import inspect
import os
import re
import sys

import psycopg
from smolagents import CodeAgent, LiteLLMModel, tool

DSN_RO = os.environ.get(
    "AIFORGE_RO_DSN",
    "postgresql://aiforge_ro@127.0.0.1:5432/aiforge",
)
LM_BASE = os.environ.get("LM_BASE", "http://127.0.0.1:1234/v1")
LM_KEY = os.environ.get("LM_KEY", "lm-studio")


def introspect_schema(conn: psycopg.Connection, tables: list[str]) -> str:
    """Return a LLM-friendly schema description for the given tables."""
    lines: list[str] = [
        "Tables (public schema, read-only):",
        "",
    ]
    with conn.cursor() as cur:
        for t in tables:
            cur.execute(
                "SELECT column_name, data_type, is_nullable "
                "FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name=%s "
                "ORDER BY ordinal_position",
                (t,),
            )
            rows = cur.fetchall()
            if not rows:
                lines.append(f"-- {t}: table not found")
                continue
            cols = ", ".join(f"{n} {dt}" for n, dt, _ in rows)
            lines.append(f"{t}({cols})")
    lines.append("")
    lines.append(
        "Notes:\n"
        "- Always add LIMIT 100 to avoid huge results.\n"
        "- `now()` returns timestamptz at the DB host timezone.\n"
        "- Only SELECT/WITH statements are permitted by the tool."
    )
    return "\n".join(lines)


def make_sql_tool(conn: psycopg.Connection):
    @tool
    def sql_engine(query: str) -> str:
        """Run a read-only SQL query on the aiforge Postgres database.

        Returns header + up to 100 rows as tab-separated values,
        or "ERROR: <reason>" on failure.

        Args:
            query: A single SELECT or WITH statement. Writes are blocked.
        """
        q = query.strip().rstrip(";")
        if not re.match(r"(?i)^(select|with)\b", q):
            return "ERROR: only SELECT/WITH statements allowed"
        if re.search(
            r"(?i)\b(insert|update|delete|drop|alter|truncate|grant|revoke)\b",
            q,
        ):
            return "ERROR: write operations blocked"
        try:
            with conn.cursor() as cur:
                cur.execute(q)
                cols = [d.name for d in cur.description or []]
                rows = cur.fetchmany(100)
            if not rows:
                return "(no rows)"
            out = ["\t".join(cols)]
            for r in rows:
                out.append("\t".join("" if v is None else str(v) for v in r))
            return "\n".join(out)
        except Exception as exc:
            return f"ERROR: {type(exc).__name__}: {exc}"

    return sql_engine


def build_agent(model_id: str, sql_engine_tool) -> CodeAgent:
    _lm_params = set(inspect.signature(LiteLLMModel.__init__).parameters)
    key = "model_id" if "model_id" in _lm_params else "model"
    mid = model_id if "/" in model_id else f"openai/{model_id}"
    model = LiteLLMModel(**{key: mid, "api_base": LM_BASE, "api_key": LM_KEY})
    return CodeAgent(
        tools=[sql_engine_tool],
        model=model,
        max_steps=8,
        additional_authorized_imports=["re", "json"],
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("question", nargs="+", help="Natural-language question")
    ap.add_argument("--model", default="qwen3.6-35b-a3b")
    ap.add_argument(
        "--tables",
        default="tickets,ticket_events,memories",
        help="Comma-separated tables to expose in the schema doc.",
    )
    args = ap.parse_args()
    question = " ".join(args.question).strip()
    tables = [t.strip() for t in args.tables.split(",") if t.strip()]

    with psycopg.connect(DSN_RO, autocommit=True, connect_timeout=5) as conn:
        schema_doc = introspect_schema(conn, tables)
        sql_engine_tool = make_sql_tool(conn)
        agent = build_agent(args.model, sql_engine_tool)

        preamble = (
            "You are an aiforge ops SQL assistant. Answer by calling the "
            "`sql_engine` tool. Do NOT invent data. Always run SQL first. "
            "When you have the answer, call `final_answer(<value>)` with a "
            "concrete number, list, or identifier. Be terse.\n\n"
            + schema_doc
            + "\n\nQuestion: "
        )
        answer = agent.run(preamble + question)
    print(answer)
    return 0


if __name__ == "__main__":
    sys.exit(main())
