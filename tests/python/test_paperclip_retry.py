from __future__ import annotations

from pathlib import Path

import pytest

from aiforge_core.config import PaperclipConfig
from aiforge_core.lifecycle import LifecycleError  # advance removed in P6.1; restored in a later task
from aiforge_core.retry import (
    BreakerTripped,
    CircuitBreaker,
    FALLBACK_THRESHOLD,
    RetryExceeded,
    enforce_loop_caps,
    pick_profile,
    require_coverage_for_mr,
    should_escalate_to_fallback,
)
from aiforge_core.store import Store

REPO_ROOT = Path(__file__).resolve().parents[2]


# --- loop caps ---
def test_dev_tester_loop_cap_escalates(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite")
    cfg = PaperclipConfig.load(REPO_ROOT)
    t = store.create_ticket("bug", "", assignee="em")
    # Walk into verifying once, loop back to coding, verifying again ... up to cap+1.
    advance(store, cfg, t.id, "planning", actor="em")
    advance(store, cfg, t.id, "tests_writing", actor="em")
    advance(store, cfg, t.id, "coding", actor="tester")
    for _ in range(cfg.retry_rules.dev_tester_loops_max):
        advance(store, cfg, t.id, "verifying", actor="sr-developer")
        advance(store, cfg, t.id, "coding", actor="tester")  # loop back — dev↔tester +1

    # One more verify→coding would exceed the cap.
    advance(store, cfg, t.id, "verifying", actor="sr-developer")
    with pytest.raises(RetryExceeded):
        advance(store, cfg, t.id, "coding", actor="tester")


def test_coverage_gate_blocks_mr(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite")
    cfg = PaperclipConfig.load(REPO_ROOT)
    t = store.create_ticket("x", "", assignee="em")
    for s in ("planning", "tests_writing", "coding", "verifying", "reviewing"):
        advance(store, cfg, t.id, s, actor="em")
    # No coverage event recorded → MR blocked.
    with pytest.raises(LifecycleError, match="no coverage"):
        advance(store, cfg, t.id, "mr_created", actor="sr-architect")


def test_coverage_below_threshold_blocks(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite")
    cfg = PaperclipConfig.load(REPO_ROOT)
    t = store.create_ticket("x", "", assignee="em")
    for s in ("planning", "tests_writing", "coding", "verifying", "reviewing"):
        advance(store, cfg, t.id, s, actor="em")
    store.audit_event(t.id, "coverage", "tester", {"pct": 42.0})
    with pytest.raises(LifecycleError, match="below minimum"):
        advance(store, cfg, t.id, "mr_created", actor="sr-architect")


def test_coverage_at_threshold_allows(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite")
    cfg = PaperclipConfig.load(REPO_ROOT)
    t = store.create_ticket("x", "", assignee="em")
    for s in ("planning", "tests_writing", "coding", "verifying", "reviewing"):
        advance(store, cfg, t.id, s, actor="em")
    store.audit_event(t.id, "coverage", "tester", {"pct": 80.0})
    advance(store, cfg, t.id, "mr_created", actor="sr-architect")
    assert store.get_ticket(t.id).state == "mr_created"


# --- circuit breaker ---
def test_breaker_trips_after_threshold(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite")
    t = store.create_ticket("x", "", assignee="sr_developer")
    cb = CircuitBreaker(store=store, threshold=3)

    cb.record_failure(t.id, "sr-developer", "syntax error")
    cb.record_failure(t.id, "sr-developer", "syntax error")
    with pytest.raises(BreakerTripped):
        cb.record_failure(t.id, "sr-developer", "syntax error")

    # trip event logged
    events = [e for e in store.list_audit(t.id) if e["event"] == "breaker_trip"]
    assert len(events) == 1


def test_breaker_reset_allows_retry(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite")
    t = store.create_ticket("x", "", assignee="sr_developer")
    cb = CircuitBreaker(store=store, threshold=2)

    cb.record_failure(t.id, "sr-developer", "oops")
    with pytest.raises(BreakerTripped):
        cb.record_failure(t.id, "sr-developer", "oops")
    cb.reset(t.id, "sr-developer", actor="human")
    # Fresh failures allowed after reset.
    cb.record_failure(t.id, "sr-developer", "oops")   # 1st after reset


def test_breaker_success_resets_count(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite")
    t = store.create_ticket("x", "", assignee="sr_developer")
    cb = CircuitBreaker(store=store, threshold=3)
    cb.record_failure(t.id, "sr-developer", "a")
    cb.record_failure(t.id, "sr-developer", "b")
    cb.record_success(t.id, "sr-developer")
    # Counter now zero. Two fresh failures must not trip.
    cb.record_failure(t.id, "sr-developer", "c")
    cb.record_failure(t.id, "sr-developer", "d")


# --- fallback routing ---
def test_fallback_false_initially(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite")
    cfg = PaperclipConfig.load(REPO_ROOT)
    t = store.create_ticket("x", "", assignee="em")
    assert should_escalate_to_fallback(store, t.id, "sr-developer") is False
    assert pick_profile(store, t.id, "sr-developer") == "sr-developer"


def test_fallback_kicks_after_two_loops(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite")
    cfg = PaperclipConfig.load(REPO_ROOT)
    t = store.create_ticket("x", "", assignee="em")
    advance(store, cfg, t.id, "planning", actor="em")
    advance(store, cfg, t.id, "tests_writing", actor="em")
    advance(store, cfg, t.id, "coding", actor="tester")
    # 2 dev↔tester loops.
    for _ in range(FALLBACK_THRESHOLD):
        advance(store, cfg, t.id, "verifying", actor="sr-developer")
        advance(store, cfg, t.id, "coding", actor="tester")
    assert should_escalate_to_fallback(store, t.id, "sr-developer") is True
    assert pick_profile(store, t.id, "sr-developer") == "sr-developer-fallback"


def test_fallback_attribution_tester_only_dev_tester(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite")
    cfg = PaperclipConfig.load(REPO_ROOT)
    t = store.create_ticket("x", "", assignee="em")
    advance(store, cfg, t.id, "planning", actor="em")
    advance(store, cfg, t.id, "tests_writing", actor="em")
    advance(store, cfg, t.id, "coding", actor="tester")
    for _ in range(FALLBACK_THRESHOLD):
        advance(store, cfg, t.id, "verifying", actor="sr-developer")
        advance(store, cfg, t.id, "coding", actor="tester")
    assert should_escalate_to_fallback(store, t.id, "tester") is True
    # sr-architect doesn't see these; only dev↔architect counts for them.
    assert should_escalate_to_fallback(store, t.id, "sr-architect") is False


from pathlib import Path
from aiforge_core.retry import kill_switch_tripped, confidence_route


def test_kill_switch_global(tmp_path):
    ks = tmp_path / "KILL"
    assert not kill_switch_tripped(str(ks), ticket_tags=[])
    ks.write_text("die")
    assert kill_switch_tripped(str(ks), ticket_tags=[])


def test_kill_switch_ticket_tag(tmp_path):
    ks = tmp_path / "KILL"
    assert not kill_switch_tripped(str(ks), ticket_tags=["ok"])
    assert kill_switch_tripped(str(ks), ticket_tags=["kill"])


def test_confidence_route_thresholds():
    assert confidence_route(0.9, 0.7, 0.5, 0.3) == "proceed"
    assert confidence_route(0.6, 0.7, 0.5, 0.3) == "retry"
    assert confidence_route(0.4, 0.7, 0.5, 0.3) == "retry"
    assert confidence_route(0.2, 0.7, 0.5, 0.3) == "escalate"
