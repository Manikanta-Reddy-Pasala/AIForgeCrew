from __future__ import annotations

from pathlib import Path

import pytest

from paperclip.config import PaperclipConfig
from paperclip.lifecycle import LifecycleError, advance, allowed_next_states
from paperclip.store import Store


REPO_ROOT = Path(__file__).resolve().parents[2]


def _store(tmp_path: Path) -> Store:
    return Store(tmp_path / "db.sqlite")


def test_full_happy_path(tmp_path: Path) -> None:
    s = _store(tmp_path)
    cfg = PaperclipConfig.load(REPO_ROOT)
    t = s.create_ticket("bug", "", assignee=cfg.routing.initial_assignee)

    advance(s, cfg, t.id, "planning",       actor="em")           # EM picks up
    assert s.get_ticket(t.id).state == "planning"

    advance(s, cfg, t.id, "tests_writing",  actor="em")           # → Tester
    assert s.get_ticket(t.id).assignee == cfg.routing.post_planning

    advance(s, cfg, t.id, "coding",         actor="tester")       # → Sr Dev
    assert s.get_ticket(t.id).assignee == cfg.routing.post_tests_ready

    advance(s, cfg, t.id, "verifying",      actor="sr-developer") # → Tester (verify)
    assert s.get_ticket(t.id).assignee == cfg.routing.post_code_ready

    advance(s, cfg, t.id, "reviewing",      actor="tester")       # → Architect
    assert s.get_ticket(t.id).assignee == cfg.routing.post_verified

    advance(s, cfg, t.id, "mr_created",     actor="sr-architect") # → human
    assert s.get_ticket(t.id).assignee == cfg.routing.on_approve

    advance(s, cfg, t.id, "merged",         actor="human")
    assert s.get_ticket(t.id).state == "merged"


def test_verify_fail_loops_back(tmp_path: Path) -> None:
    s = _store(tmp_path)
    cfg = PaperclipConfig.load(REPO_ROOT)
    t = s.create_ticket("bug", "", assignee=cfg.routing.initial_assignee)
    for step in ("planning", "tests_writing", "coding", "verifying"):
        advance(s, cfg, t.id, step, actor="em")
    advance(s, cfg, t.id, "coding", actor="tester")  # tests fail → back to Dev
    assert s.get_ticket(t.id).state == "coding"
    assert s.get_ticket(t.id).assignee == cfg.routing.post_tests_ready


def test_invalid_transition_raises(tmp_path: Path) -> None:
    s = _store(tmp_path)
    cfg = PaperclipConfig.load(REPO_ROOT)
    t = s.create_ticket("bug", "", assignee=cfg.routing.initial_assignee)
    # skip-ahead: created → reviewing is not allowed.
    with pytest.raises(LifecycleError):
        advance(s, cfg, t.id, "reviewing", actor="em")


def test_allowed_states() -> None:
    assert allowed_next_states("created") == ["planning"]
    assert set(allowed_next_states("verifying")) == {"reviewing", "coding", "escalated"}
    assert allowed_next_states("merged") == []
