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
    """Persisted cost rollup — SQLite-degraded no-op.

    ``group_by`` ∈ {"day", "role", "model", "ticket"}. The persisted rollup
    was backed by a Postgres ``llm_costs`` table; Postgres has been removed
    (SQLite-only build), so this returns an empty list. Live in-memory totals
    are still available via :func:`snapshot`. The ``group_by`` contract is
    still validated so a bad value fails loudly for the caller.
    """
    # unused, deliberately: the Postgres cost tables are gone; the signature stays so callers degrade quietly.
    del days_back
    if group_by not in ("day", "role", "model", "ticket"):
        raise ValueError("group_by must be day|role|model|ticket")
    return []


# ───────── helpers ─────────────────────────────────────────────────


def _short_model_name(model: str) -> str:
    """Strip LiteLLM prefixes + leading filesystem path."""
    if "/" in model and not model.startswith("openai/"):
        return model.rsplit("/", 1)[-1]
    for p in ("openai/", "ollama/", "ollama_cloud/"):
        if model.startswith(p):
            return model[len(p):]
    return model


def _persist(
    *, role: str, ticket: Optional[str], model: str, cost_usd: float,
    prompt_tokens: int, completion_tokens: int,
) -> None:
    """Persist one cost row — SQLite-degraded no-op.

    Per-call cost rows were appended to a Postgres ``llm_costs`` table.
    Postgres has been removed (SQLite-only build), so persistence is a no-op;
    live totals stay in memory (see :func:`snapshot`). ``record_call`` already
    treats this write as best-effort.
    """
    # unused, deliberately: the Postgres cost tables are gone; the signature stays so callers degrade quietly.
    del role, ticket, model, cost_usd, prompt_tokens, completion_tokens
    return None
