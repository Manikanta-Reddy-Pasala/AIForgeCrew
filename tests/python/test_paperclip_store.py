from __future__ import annotations

from pathlib import Path

import pytest

from paperclip.store import Store


def test_create_and_get(tmp_path: Path) -> None:
    s = Store(tmp_path / "db.sqlite")
    t = s.create_ticket("login bug", "steps...", assignee="engineering_manager")
    assert t.id.startswith("TICKET-")
    got = s.get_ticket(t.id)
    assert got is not None
    assert got.title == "login bug"
    assert got.assignee == "engineering_manager"
    assert got.state == "created"


def test_comments_ordered(tmp_path: Path) -> None:
    s = Store(tmp_path / "db.sqlite")
    t = s.create_ticket("t", "", assignee="em")
    s.add_comment(t.id, "em", "first")
    s.add_comment(t.id, "tester", "second")
    comments = s.list_comments(t.id)
    assert [c.body for c in comments] == ["first", "second"]
    assert [c.author for c in comments] == ["em", "tester"]


def test_audit_events_captured(tmp_path: Path) -> None:
    s = Store(tmp_path / "db.sqlite")
    t = s.create_ticket("t", "", assignee="em")
    s.transition(t.id, "planning", actor="em")
    s.assign(t.id, "tester", actor="em")
    s.add_comment(t.id, "tester", "tests ready")
    events = s.list_audit(t.id)
    kinds = [e["event"] for e in events]
    assert kinds == ["create", "transition", "assign", "comment"]


def test_invalid_state_rejected(tmp_path: Path) -> None:
    s = Store(tmp_path / "db.sqlite")
    t = s.create_ticket("t", "", assignee="em")
    with pytest.raises(ValueError):
        s.transition(t.id, "quantum_mode", actor="em")


def test_list_filters(tmp_path: Path) -> None:
    s = Store(tmp_path / "db.sqlite")
    t1 = s.create_ticket("a", "", assignee="em")
    t2 = s.create_ticket("b", "", assignee="tester")
    s.transition(t2.id, "planning", actor="em")
    assert [t.id for t in s.list_tickets(assignee="tester")] == [t2.id]
    assert [t.id for t in s.list_tickets(state="created")] == [t1.id]
