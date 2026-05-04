"""$/turn $/ticket cost tracker — KISS rate table + Postgres rollup.

Token counters live elsewhere (``ga_tools/tokens.py`` for Doer,
``otel.record_token_usage`` for tracing). This module owns the
**dollar mapping**: model → ($/Mtok prompt, $/Mtok completion). Calls
:func:`record_call` after each LLM round-trip, persists per-ticket
totals, exposes :func:`snapshot` for the UI gauge.

KISS:
- Single rate table (``RATES`` dict). Add new model = one line.
- Best-effort Postgres write — silent on failure (cost data is
  observability, not load-bearing).
- ``AIFORGE_COST_TRACKING=0`` disables (default on).

Public surface:
- ``record_call(role, ticket, model, prompt_tokens, completion_tokens)``
- ``snapshot(ticket=None) -> dict``
- ``reset_table(model: str, prompt_per_mtok: float, completion_per_mtok: float)``
"""
from __future__ import annotations

import os
import threading
from typing import Optional


# USD per 1M tokens. Values reflect public list price as of 2026-04;
# operator can override at runtime via :func:`reset_table`.
RATES: dict[str, tuple[float, float]] = {
    # OpenAI / OpenAI-compat
    "gpt-oss:120b":            (0.50, 1.50),
    "gpt-oss:20b":             (0.10, 0.30),
    # Ollama Cloud names (per their pricing page)
    "qwen3-coder-next":        (0.20, 0.60),
    "glm-4.7":                 (0.30, 0.90),
    "glm-5":                   (0.50, 1.50),
    "kimi-k2.6":               (0.40, 1.20),
    "kimi-k2:1t":              (1.50, 4.50),
    # Anthropic
    "claude-sonnet-4-5":       (3.00, 15.00),
    "claude-opus-4-7":         (15.00, 75.00),
    # Gemini
    "gemini-2.5-flash":        (0.075, 0.30),
    "gemini-2.5-pro":          (1.25, 5.00),
    # Local mlx-lm (free)
    "Qwen3-Coder-Next-MLX-4bit": (0.0, 0.0),
}

_LOCK = threading.Lock()
_TOTALS: dict[str, dict] = {}  # ticket → {usd, prompt, completion, calls}
_GLOBAL = {"usd": 0.0, "prompt": 0, "completion": 0, "calls": 0}


def reset_table(
    model: str, prompt_per_mtok: float, completion_per_mtok: float,
) -> None:
    """Override one model's pricing at runtime."""
    RATES[model] = (float(prompt_per_mtok), float(completion_per_mtok))


def usd_for(
    model: str, *, prompt_tokens: int, completion_tokens: int,
) -> float:
    """Compute USD cost. Returns 0.0 for unknown models so the gauge
    never crashes the run path."""
    short = _short_model_name(model)
    rate = RATES.get(short)
    if rate is None:
        return 0.0
    p, c = rate
    return (prompt_tokens / 1_000_000) * p + (completion_tokens / 1_000_000) * c


def record_call(
    *, role: str, ticket: Optional[str], model: str,
    prompt_tokens: int, completion_tokens: int,
) -> dict:
    """Tally one LLM call. Returns delta + cumulative ticket total."""
    if os.environ.get("AIFORGE_COST_TRACKING", "1") != "1":
        return {"usd": 0.0, "ticket_total": 0.0}
    cost = usd_for(
        model,
        prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
    )
    with _LOCK:
        _GLOBAL["usd"] += cost
        _GLOBAL["prompt"] += prompt_tokens
        _GLOBAL["completion"] += completion_tokens
        _GLOBAL["calls"] += 1
        if ticket:
            row = _TOTALS.setdefault(
                ticket,
                {"usd": 0.0, "prompt": 0, "completion": 0, "calls": 0},
            )
            row["usd"] += cost
            row["prompt"] += prompt_tokens
            row["completion"] += completion_tokens
            row["calls"] += 1
            ticket_total = row["usd"]
        else:
            ticket_total = 0.0
    # Best-effort persist — ignore DB failures.
    try:
        _persist(role=role, ticket=ticket, model=model, cost_usd=cost,
                 prompt_tokens=prompt_tokens,
                 completion_tokens=completion_tokens)
    except Exception:
        pass
    return {"usd": cost, "ticket_total": ticket_total}


def snapshot(ticket: Optional[str] = None) -> dict:
    """Return current cost totals."""
    with _LOCK:
        if ticket:
            return dict(_TOTALS.get(ticket, {
                "usd": 0.0, "prompt": 0, "completion": 0, "calls": 0,
            }))
        return {"global": dict(_GLOBAL), "tickets": dict(_TOTALS)}


def rollup(group_by: str = "day", *, days_back: int = 30) -> list[dict]:
    """SQL rollup over the ``llm_costs`` table.

    ``group_by`` ∈ {"day", "role", "model", "ticket"} — KISS, one
    GROUP BY clause per call. Returns ``[{key, calls, prompt_tokens,
    completion_tokens, cost_usd}, ...]``. Empty list when the
    table doesn't exist yet (cost tracking off / never fired).
    """
    if group_by not in ("day", "role", "model", "ticket"):
        raise ValueError(f"group_by must be day|role|model|ticket")
    expr = {
        "day":    "to_char(date_trunc('day', created_at), 'YYYY-MM-DD')",
        "role":   "COALESCE(role, '?')",
        "model":  "COALESCE(model, '?')",
        "ticket": "COALESCE(ticket, '?')",
    }[group_by]
    sql = (
        f"SELECT {expr} AS key, "
        " COUNT(*) AS calls,"
        " COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,"
        " COALESCE(SUM(completion_tokens), 0) AS completion_tokens,"
        " COALESCE(SUM(cost_usd), 0)::float AS cost_usd "
        " FROM llm_costs "
        " WHERE created_at > NOW() - (%s || ' days')::interval "
        f"GROUP BY {expr} "
        " ORDER BY cost_usd DESC "
        " LIMIT 200"
    )
    try:
        import psycopg
        from aiforge_core.config.env import AIFORGE_DSN
        with psycopg.connect(AIFORGE_DSN, connect_timeout=2) as c, \
             c.cursor() as cur:
            cur.execute(sql, (str(days_back),))
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception:
        return []


# ───────── helpers ─────────────────────────────────────────────────


def _short_model_name(model: str) -> str:
    """Strip LiteLLM prefixes + leading filesystem path."""
    if "/" in model and not model.startswith(("openai/", "anthropic/")):
        return model.rsplit("/", 1)[-1]
    for p in ("openai/", "anthropic/", "ollama/", "ollama_cloud/"):
        if model.startswith(p):
            return model[len(p):]
    return model


def _persist(
    *, role: str, ticket: Optional[str], model: str, cost_usd: float,
    prompt_tokens: int, completion_tokens: int,
) -> None:
    """Append one row to ``llm_costs`` Postgres table.

    Schema (auto-created on first call):
        CREATE TABLE IF NOT EXISTS llm_costs (
          id BIGSERIAL PRIMARY KEY,
          created_at TIMESTAMPTZ DEFAULT now(),
          ticket TEXT, role TEXT, model TEXT,
          prompt_tokens INT, completion_tokens INT,
          cost_usd NUMERIC(10,6)
        );
    """
    import psycopg
    from aiforge_core.config.env import AIFORGE_DSN
    with psycopg.connect(AIFORGE_DSN, connect_timeout=2) as c, \
         c.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS llm_costs ("
            " id BIGSERIAL PRIMARY KEY,"
            " created_at TIMESTAMPTZ DEFAULT now(),"
            " ticket TEXT, role TEXT, model TEXT,"
            " prompt_tokens INT, completion_tokens INT,"
            " cost_usd NUMERIC(10,6))"
        )
        cur.execute(
            "INSERT INTO llm_costs"
            " (ticket, role, model, prompt_tokens,"
            "  completion_tokens, cost_usd)"
            " VALUES (%s,%s,%s,%s,%s,%s)",
            (ticket, role, model,
             prompt_tokens, completion_tokens, cost_usd),
        )
        c.commit()
