from __future__ import annotations

from pathlib import Path

from paperclip.budget import Spend, record
from paperclip.config import PaperclipConfig
from paperclip.lifecycle import advance
from paperclip.observe import fleet_summary, ticket_report
from paperclip.store import Store

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_ticket_report_aggregates(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite")
    cfg = PaperclipConfig.load(REPO_ROOT)

    t = store.create_ticket("x", "body", assignee=cfg.routing.initial_assignee)

    # Spend + transitions + a tool_call.
    record(store, t.id, Spend(role="em", tokens=1200))
    record(store, t.id, Spend(role="tester", tokens=3400))
    store.audit_event(t.id, "tool_call", "tester", {"tool": "run_tests", "ok": True})
    advance(store, cfg, t.id, "planning", actor="em")
    advance(store, cfg, t.id, "tests_writing", actor="em")

    r = ticket_report(store, t.id)
    assert r is not None
    assert r.tokens_per_role == {"em": 1200, "tester": 3400}
    assert r.tool_calls_per_role == {"tester": 1}
    assert len(r.transitions) == 2
    assert r.escalated is False


def test_loop_counts(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite")
    cfg = PaperclipConfig.load(REPO_ROOT)
    t = store.create_ticket("x", "", assignee=cfg.routing.initial_assignee)

    # Walk into verifying, then back to coding (dev↔tester loop).
    for s in ("planning", "tests_writing", "coding", "verifying"):
        advance(store, cfg, t.id, s, actor="em")
    advance(store, cfg, t.id, "coding", actor="tester")           # dev↔tester loop #1
    advance(store, cfg, t.id, "verifying", actor="sr-developer")
    advance(store, cfg, t.id, "reviewing", actor="tester")
    advance(store, cfg, t.id, "coding", actor="sr-architect")     # dev↔architect loop #1

    r = ticket_report(store, t.id)
    assert r is not None
    assert r.loops["dev_tester"] == 1
    assert r.loops["dev_architect"] == 1


def test_fleet_summary(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite")
    cfg = PaperclipConfig.load(REPO_ROOT)
    store.create_ticket("a", "", assignee="em")
    store.create_ticket("b", "", assignee="tester")

    s = fleet_summary(store, cfg)
    assert s["total_tickets"] == 2
    assert s["by_state"]["created"] == 2
    assert "em" in s["budgets"]
    assert "tokens_per_ticket" in s["budgets"]["em"]
