"""Tests for the perf recorder (ndjson append + aggregate + reset + timed)."""

from __future__ import annotations

import json
import os

import pytest

from aiforge_core.runtime import perf_recorder


@pytest.fixture(autouse=True)
def _isolated_config_dir(tmp_path, monkeypatch):
    """Point the recorder at a tmp config dir so tests are hermetic."""
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    yield


def test_record_creates_ndjson(tmp_path):
    perf_recorder.record("LLM", "developer", 12.5)
    path = tmp_path / "perf.ndjson"
    assert path.exists()
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["family"] == "LLM"
    assert rec["name"] == "developer"
    assert rec["ms"] == 12.5
    assert "ts" in rec


def test_aggregate_shape_and_math():
    perf_recorder.record("LLM", "developer", 100.0)
    perf_recorder.record("LLM", "developer", 300.0)
    perf_recorder.record("Tool", "run_command", 50.0)

    rows = perf_recorder.aggregate()
    by_key = {(r["event"], r["name"]): r for r in rows}

    dev = by_key[("LLM", "developer")]
    assert dev["count"] == 2
    assert dev["total_ms"] == 400.0
    assert dev["max_ms"] == 300.0

    tool = by_key[("Tool", "run_command")]
    assert tool["count"] == 1
    assert tool["total_ms"] == 50.0
    assert tool["max_ms"] == 50.0

    # Sorted by total_ms desc → developer (400) before run_command (50).
    assert rows[0]["name"] == "developer"


def test_aggregate_empty_when_no_file():
    assert perf_recorder.aggregate() == []


def test_reset_empties(tmp_path):
    perf_recorder.record("File", "file_read", 5.0)
    assert perf_recorder.aggregate()
    perf_recorder.reset()
    assert perf_recorder.aggregate() == []
    # File truncated, not deleted.
    assert (tmp_path / "perf.ndjson").read_text() == ""


def test_aggregate_skips_bad_lines(tmp_path):
    path = tmp_path / "perf.ndjson"
    path.write_text(
        json.dumps({"family": "Tool", "name": "x", "ms": 10.0}) + "\n"
        + "not json at all\n"
        + "\n"
        + json.dumps({"family": "Tool", "name": "x", "ms": 20.0}) + "\n"
    )
    rows = perf_recorder.aggregate()
    assert len(rows) == 1
    assert rows[0]["count"] == 2
    assert rows[0]["total_ms"] == 30.0


def test_record_soft_fails_on_bad_path(monkeypatch, tmp_path):
    # Make the config dir a path that cannot become a directory (a file).
    bad = tmp_path / "afile"
    bad.write_text("x")
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(bad / "sub"))
    # Force makedirs to blow up by pointing into a file; must not raise.
    monkeypatch.setattr(perf_recorder.os, "makedirs",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    perf_recorder.record("LLM", "x", 1.0)  # should swallow


def test_timed_records_elapsed(tmp_path):
    with perf_recorder.timed("Tool", "slow_op"):
        pass
    rows = perf_recorder.aggregate()
    assert len(rows) == 1
    assert rows[0]["event"] == "Tool"
    assert rows[0]["name"] == "slow_op"
    assert rows[0]["count"] == 1
    assert rows[0]["total_ms"] >= 0.0


def test_timed_records_on_exception(tmp_path):
    with pytest.raises(ValueError):
        with perf_recorder.timed("LLM", "boom"):
            raise ValueError("nope")
    rows = perf_recorder.aggregate()
    assert len(rows) == 1
    assert rows[0]["name"] == "boom"
