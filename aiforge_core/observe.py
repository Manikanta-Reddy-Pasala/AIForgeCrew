"""Observability — aggregate audit rows into per-ticket and per-role metrics.

All metrics derive from the audit table (append-only, §9.3). This module
produces dicts suitable for CLI report output + JSON dumps for a future
dashboard (panels declared in observability/dashboard-config.yml).
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from .config import PaperclipConfig
from .store import Store


@dataclass
class TicketReport:
    ticket_id: str
    title: str
    state: str
    assignee: str
    created_at: float
    updated_at: float
    duration_s: float
    tokens_per_role: dict[str, int]
    tool_calls_per_role: dict[str, int]
    transitions: list[dict]
    loops: dict[str, int]          # "dev_tester" / "dev_architect"
    comment_count: int
    escalated: bool

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


def ticket_report(store: Store, ticket_id: str) -> TicketReport | None:
    t = store.get_ticket(ticket_id)
    if t is None:
        return None
    events = store.list_audit(ticket_id)

    tokens: Counter[str] = Counter()
    tools: Counter[str] = Counter()
    transitions: list[dict] = []
    loops = {"dev_tester": 0, "dev_architect": 0}

    for e in events:
        if e["event"] == "budget":
            tokens[e["actor"]] += int(e["data"].get("tokens") or 0)
        elif e["event"] == "tool_call":
            tools[e["actor"]] += 1
        elif e["event"] == "transition":
            transitions.append({
                "from": e["data"].get("from"),
                "to": e["data"].get("to"),
                "actor": e["actor"],
                "at": e["created_at"],
            })
            # Loop-back detection — when tests fail, verifying → coding (back to Dev)
            # When review rejects, reviewing → coding.
            if e["data"].get("from") == "verifying" and e["data"].get("to") == "coding":
                loops["dev_tester"] += 1
            if e["data"].get("from") == "reviewing" and e["data"].get("to") == "coding":
                loops["dev_architect"] += 1

    return TicketReport(
        ticket_id=t.id,
        title=t.title,
        state=t.state,
        assignee=t.assignee,
        created_at=t.created_at,
        updated_at=t.updated_at,
        duration_s=t.updated_at - t.created_at,
        tokens_per_role=dict(tokens),
        tool_calls_per_role=dict(tools),
        transitions=transitions,
        loops=loops,
        comment_count=len(store.list_comments(ticket_id)),
        escalated=(t.state == "escalated"),
    )


def fleet_summary(store: Store, cfg: PaperclipConfig) -> dict:
    """Aggregate metrics across all tickets — snapshot for a dashboard."""
    tickets = store.list_tickets()
    by_state: Counter[str] = Counter()
    total_tokens: Counter[str] = Counter()
    total_tool_calls: Counter[str] = Counter()
    stalled = []  # tickets over stale_ticket_timeout_minutes per retry_rules

    import time
    now = time.time()
    stale_threshold = cfg.retry_rules.stale_ticket_timeout_minutes * 60

    for t in tickets:
        by_state[t.state] += 1
        if t.state not in ("merged", "escalated") and (now - t.updated_at) > stale_threshold:
            stalled.append({"id": t.id, "state": t.state, "stale_s": int(now - t.updated_at)})
        # Collect per-role spend/tool counts.
        report = ticket_report(store, t.id)
        if report:
            for role, n in report.tokens_per_role.items():
                total_tokens[role] += n
            for role, n in report.tool_calls_per_role.items():
                total_tool_calls[role] += n

    return {
        "total_tickets": len(tickets),
        "by_state": dict(by_state),
        "tokens_per_role": dict(total_tokens),
        "tool_calls_per_role": dict(total_tool_calls),
        "stalled_tickets": stalled,
        "budgets": {
            role: {
                "tokens_per_ticket": b.tokens_per_ticket,
                "cloud_usd_per_month": b.cloud_usd_per_month,
            }
            for role, b in cfg.budgets.items()
        },
    }
