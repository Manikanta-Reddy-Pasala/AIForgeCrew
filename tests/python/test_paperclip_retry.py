"""Retry helpers for lifecycle v4.1 — kill switch + confidence route."""
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
