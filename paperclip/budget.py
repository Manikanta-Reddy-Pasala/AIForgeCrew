"""Per-agent token + cloud-$ budgets + circuit breaker per DESIGN.md §10.

State lives in the audit table (`event='budget'`). Each spend is idempotent-logged
against a ticket; the tripper reads current ticket + month totals.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from .config import PaperclipConfig
from .store import Store


class BudgetExceeded(RuntimeError):
    """Circuit breaker tripped — kill current call + alert human."""


@dataclass
class Spend:
    role: str
    tokens: int = 0
    usd: float = 0.0


def record(store: Store, ticket_id: str, spend: Spend) -> None:
    store.audit_event(
        ticket_id,
        "budget",
        spend.role,
        {"tokens": spend.tokens, "usd": spend.usd},
    )


def ticket_tokens(store: Store, ticket_id: str, role: str) -> int:
    total = 0
    for e in store.list_audit(ticket_id):
        if e["event"] == "budget" and e["actor"] == role:
            total += int(e["data"].get("tokens") or 0)
    return total


def month_usd(store: Store, role: str, now: float | None = None) -> float:
    """Sum all USD spend for `role` in the current calendar month."""
    import datetime as dt

    now = now or time.time()
    d = dt.datetime.fromtimestamp(now, tz=dt.timezone.utc)
    month_start = dt.datetime(d.year, d.month, 1, tzinfo=dt.timezone.utc).timestamp()

    cur = store._conn.execute(
        "SELECT data_json FROM audit "
        "WHERE event='budget' AND actor=? AND created_at>=?",
        (role, month_start),
    )
    import json
    return sum(float(json.loads(r["data_json"]).get("usd") or 0.0) for r in cur.fetchall())


def assert_within_budget(cfg: PaperclipConfig, store: Store, ticket_id: str, role: str, next_spend: Spend) -> None:
    b = cfg.budgets.get(role.replace("-", "_"))
    if b is None:
        return  # role has no declared budget — silent allow (e.g. human/CEO)

    used = ticket_tokens(store, ticket_id, role) + int(next_spend.tokens or 0)
    if used > b.tokens_per_ticket:
        raise BudgetExceeded(
            f"{role} ticket-token cap {b.tokens_per_ticket} exceeded "
            f"on {ticket_id} (used={used})"
        )

    if b.cloud_usd_per_month is not None:
        spent_month = month_usd(store, role) + float(next_spend.usd or 0.0)
        if spent_month > b.cloud_usd_per_month:
            raise BudgetExceeded(
                f"{role} monthly cloud-USD cap ${b.cloud_usd_per_month} exceeded "
                f"(month={spent_month:.2f})"
            )
