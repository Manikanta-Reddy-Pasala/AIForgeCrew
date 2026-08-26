"""An admin that is down is normal operation, and must read like it.

Before this, every failed cycle logged a line forever: a laptop away from the
office for a week produced hundreds of identical lines, and none of them
distinguished "the admin is off" from "this machine is broken".
"""
from __future__ import annotations

import logging

import pytest

from aiforge_core.memory.sync import status


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Isolate the config dir, the quiet state, and the log path.

    ``api.py`` sets ``propagate = False`` on the ``aiforge`` logger so that its
    diagnostics print regardless of uvicorn's config. That is correct for the
    product and invisible to caplog, whose handler sits on the root logger — so
    once any test in the run has imported the API, every assertion here about a
    log line silently passes on zero records. Propagation is restored for the
    duration of each test rather than the product changed.
    """
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    family = logging.getLogger("aiforge")
    monkeypatch.setattr(family, "propagate", True)
    status.reset()
    yield
    status.reset()


# ── the record ───────────────────────────────────────────────────────────

def test_the_record_round_trips():
    status.record(state="ok", admin="http://nuc:8799", reachable=True,
                  group="cellular", groups_available=["cellular", "retail"],
                  pending=3, pushed=12)
    row = status.read()
    assert row["state"] == "ok"
    assert row["group"] == "cellular"
    assert row["groups_available"] == ["cellular", "retail"]
    assert row["pending"] == 3
    assert row["pushed_total"] == 12
    assert row["last_ok"]


def test_pending_falls_to_zero_when_everything_is_sent():
    """Pending is computed from the offer, not queued — so it cannot drift."""
    status.record(state="ok", admin="a", reachable=True, group="", pending=5)
    status.record(state="ok", admin="a", reachable=True, group="", pending=0)
    assert status.read()["pending"] == 0


def test_pushed_total_accumulates():
    status.record(state="ok", admin="a", reachable=True, pushed=3)
    status.record(state="ok", admin="a", reachable=True, pushed=4)
    assert status.read()["pushed_total"] == 7


def test_a_failure_keeps_the_last_good_timestamp():
    """'We have not synced since 14:02' is the question an operator asks."""
    status.record(state="ok", admin="a", reachable=True, group="")
    was = status.read()["last_ok"]
    status.record(state="unreachable", admin="a", reachable=False, group="",
                  error="ConnectError: refused")
    row = status.read()
    assert row["last_ok"] == was
    assert row["last_error"] == "ConnectError: refused"


def test_a_success_clears_the_last_error():
    status.record(state="unreachable", admin="a", reachable=False,
                  error="ConnectError: refused")
    status.record(state="ok", admin="a", reachable=True)
    assert status.read()["last_error"] is None


def test_an_omitted_group_list_is_carried_forward_not_erased():
    status.record(state="ok", admin="a", reachable=True,
                  groups_available=["cellular"])
    status.record(state="unreachable", admin="a", reachable=False)
    assert status.read()["groups_available"] == ["cellular"]


# ── the quiet ────────────────────────────────────────────────────────────

def test_repeated_failures_log_once_not_once_per_cycle(caplog):
    caplog.set_level(logging.WARNING, logger="aiforge.sync")
    for _ in range(20):
        status.note_failure("http://nuc:8799", "ConnectError: refused")
    assert len([r for r in caplog.records if "unreachable" in r.message]) == 1


def test_a_different_error_logs_immediately(caplog):
    """"Refused" becoming "401" is news, not more of the same."""
    caplog.set_level(logging.WARNING, logger="aiforge.sync")
    status.note_failure("a", "ConnectError: refused")
    status.note_failure("a", "HTTPStatusError: 401")
    assert len([r for r in caplog.records if "unreachable" in r.message]) == 2


def test_recovery_logs_exactly_one_line(caplog):
    status.note_failure("http://nuc:8799", "ConnectError: refused")
    caplog.set_level(logging.INFO, logger="aiforge.sync")
    status.note_success("http://nuc:8799")
    status.note_success("http://nuc:8799")
    assert len([r for r in caplog.records if "reachable again" in r.message]) == 1


def test_a_success_on_an_admin_that_never_failed_logs_nothing(caplog):
    caplog.set_level(logging.INFO, logger="aiforge.sync")
    status.note_success("a")
    assert [r for r in caplog.records if "reachable again" in r.message] == []


def test_a_continuing_outage_speaks_up_again_after_an_hour(monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="aiforge.sync")
    clock = [1000.0]
    monkeypatch.setattr(status.time, "monotonic", lambda: clock[0])

    status.note_failure("a", "boom")
    for _ in range(50):
        status.note_failure("a", "boom")
    clock[0] += status.QUIET_SECONDS + 1
    status.note_failure("a", "boom")

    lines = [r for r in caplog.records if "unreachable" in r.message]
    assert len(lines) == 2
    assert "still unreachable" in lines[1].message


# ── the block ring ───────────────────────────────────────────────────────

def test_the_block_log_records_the_rule_but_not_the_node():
    status.record_block("O-02", "secrets.aws_key", "shaped like an aws key")
    rows = status.blocks()
    assert rows[0]["rule"] == "secrets.aws_key"
    assert rows[0]["key"] == "O-02"
    assert "AKIA" not in str(rows)


def test_the_block_log_is_bounded():
    for i in range(status.MAX_BLOCKS + 25):
        status.record_block(f"O-{i}", "noise.thin", "too short")
    assert len(status.blocks()) == status.MAX_BLOCKS


def test_the_record_carries_a_count_per_rule():
    status.record_block("O-01", "secrets.aws_key", "x")
    status.record_block("O-02", "noise.thin", "y")
    status.record_block("O-03", "noise.thin", "y")
    row = status.record(state="ok", admin="a", reachable=True)
    assert row["blocked"] == {"secrets.aws_key": 1, "noise.thin": 2}


def test_a_node_held_back_every_cycle_is_counted_once():
    """The filter runs every cycle, so a held-back node stays held back.
    Appending would make "blocked: 47" mean one note seen 47 times."""
    for _ in range(20):
        status.record_block("O-02", "secrets.aws_key", "shaped like an aws key")

    assert len(status.blocks()) == 1
    assert status.record(state="ok", admin="a", reachable=True)["blocked"] == {
        "secrets.aws_key": 1}


def test_the_same_node_under_a_different_rule_is_its_own_row():
    status.record_block("O-02", "secrets.aws_key", "x")
    status.record_block("O-02", "noise.thin", "y")
    assert len(status.blocks()) == 2


def test_a_refreshed_block_moves_to_the_front():
    """Most-recent-first has to mean most recently SEEN, or a note held back
    for months sinks below one held back once and never again."""
    status.record_block("O-01", "noise.thin", "x")
    status.record_block("O-02", "noise.thin", "y")
    status.record_block("O-01", "noise.thin", "x")

    assert [b["key"] for b in status.blocks()] == ["O-01", "O-02"]


def test_an_unreachable_record_carries_the_reason_the_transport_saw():
    """"Unreachable" with no reason is the one thing the settings panel must
    not show: the reason is the only actionable half."""
    status.note_failure("http://nuc:8799", "ConnectError: connection refused")
    row = status.record(state="unreachable", admin="http://nuc:8799",
                        reachable=False, group="cellular")

    assert row["last_error"] == "ConnectError: connection refused"


def test_an_explicit_error_still_wins_over_the_transport_one():
    status.note_failure("a", "ConnectError: refused")
    row = status.record(state="unreachable", admin="a", reachable=False,
                        error="something the cycle itself saw")

    assert row["last_error"] == "something the cycle itself saw"
