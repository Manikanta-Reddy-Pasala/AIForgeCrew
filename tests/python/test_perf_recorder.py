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
    cm = perf_recorder.timed("LLM", "boom")
    with pytest.raises(ValueError):
        with cm:
            raise ValueError("nope")
    rows = perf_recorder.aggregate()
    assert len(rows) == 1
    assert rows[0]["name"] == "boom"


def test_maybe_trim_concurrent_no_corruption(tmp_path, monkeypatch):
    """CC2 — concurrent trims + appends must never produce a torn/corrupted
    file. The atomic temp-file + os.replace swap guarantees readers see a whole
    file; every surviving line stays valid JSON."""
    import threading

    # Shrink the cap so trimming triggers on a small file.
    monkeypatch.setattr(perf_recorder, "_MAX_BYTES", 2000)
    monkeypatch.setattr(perf_recorder, "_TRIM_KEEP", 10)   # kept lines < cap
    path = tmp_path / "perf.ndjson"
    with open(path, "w", encoding="utf-8") as fh:
        for i in range(500):                       # seed well past the cap
            fh.write(json.dumps({"family": "LLM", "name": "x",
                                 "ms": float(i), "ts": 0.0}) + "\n")

    errors: list = []

    def trimmer():
        try:
            for _ in range(30):
                perf_recorder._maybe_trim(str(path))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def appender():
        try:
            for _ in range(60):
                perf_recorder.record("LLM", "y", 1.0)   # writes to same path
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = ([threading.Thread(target=trimmer) for _ in range(4)]
               + [threading.Thread(target=appender) for _ in range(4)])
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    # No torn writes: every non-blank line parses as JSON.
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            json.loads(line)
    # A final trim brings it under the cap (atomic last-write-wins).
    perf_recorder._maybe_trim(str(path))
    assert path.stat().st_size <= perf_recorder._MAX_BYTES


# ── window, p95, retention, cross-process lock (2026-09-11) ────────────────

def _write(path, recs):
    path.write_text("".join(json.dumps(r) + "\n" for r in recs))


def test_snapshot_only_counts_the_window(tmp_path):
    import time
    now = time.time()
    _write(tmp_path / "perf.ndjson", [
        {"family": "LLM", "name": "doer", "ms": 9000.0, "ts": now - 3 * 86400},
        {"family": "LLM", "name": "doer", "ms": 100.0, "ts": now - 60},
    ])
    snap = perf_recorder.snapshot(86400)
    assert snap["samples"] == 1
    assert snap["rows"][0]["max_ms"] == 100.0
    assert perf_recorder.snapshot(0)["samples"] == 2          # 0 = everything


def test_rows_carry_avg_and_p95(tmp_path):
    for ms in range(1, 101):
        perf_recorder.record("Tool", "grep", float(ms))
    row = perf_recorder.snapshot()["rows"][0]
    assert row["count"] == 100
    assert row["avg_ms"] == 50.5
    assert row["p95_ms"] == 95.0
    assert row["max_ms"] == 100.0


def test_trim_drops_samples_past_retention_not_a_line_count(tmp_path, monkeypatch):
    import time
    now = time.time()
    path = tmp_path / "perf.ndjson"
    _write(path, [{"family": "LLM", "name": "old", "ms": 1.0, "ts": now - 8 * 86400}]
           + [{"family": "LLM", "name": "new", "ms": 1.0, "ts": now}] * 30)
    perf_recorder._maybe_trim(str(path))
    names = {json.loads(ln)["name"] for ln in path.read_text().splitlines()}
    assert names == {"new"}
    assert len(path.read_text().splitlines()) == 30           # recent kept whole


def test_the_first_record_of_a_process_checks_retention(tmp_path, monkeypatch):
    import time
    path = tmp_path / "perf.ndjson"
    _write(path, [{"family": "LLM", "name": "old", "ms": 1.0,
                   "ts": time.time() - 30 * 86400}])
    monkeypatch.setattr(perf_recorder, "_record_count", 0)
    perf_recorder.record("LLM", "new", 1.0)
    assert "old" not in path.read_text()


def test_appends_from_another_process_survive_a_trim(tmp_path, monkeypatch):
    """The trim's read-rewrite and every append share one flock, so a sample
    written by a second process between the read and the replace is not lost."""
    import subprocess
    import sys
    import time
    monkeypatch.setattr(perf_recorder, "_MAX_BYTES", 500)
    monkeypatch.setattr(perf_recorder, "_TRIM_KEEP", 100000)
    path = tmp_path / "perf.ndjson"
    _write(path, [{"family": "LLM", "name": "seed", "ms": 1.0, "ts": time.time()}] * 50)
    child = subprocess.Popen(
        [sys.executable, "-c",
         "from aiforge_core.runtime import perf_recorder as p\n"
         "for _ in range(300): p.record('Tool', 'child', 1.0)"],
        env={**os.environ, "AIFORGE_CONFIG_DIR": str(tmp_path)})
    while child.poll() is None:
        perf_recorder._maybe_trim(str(path))
    assert child.returncode == 0
    lines = path.read_text().splitlines()
    assert sum(1 for ln in lines if '"child"' in ln) == 300
