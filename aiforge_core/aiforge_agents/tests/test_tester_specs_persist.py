"""Tester specs persistence (#7)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from aiforge_core.aiforge_agents.orchestrator import run_ticket as rt
from aiforge_core.aiforge_agents.learner import online as learner


class _NoopLogger:
    """Replacement for the run logger — emit() pulls .info()."""

    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def info(self, *a, **kw):  # noqa: D401, ARG002
        pass


@pytest.fixture
def tmp_runs(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_RUNS_DIR", str(tmp_path))
    return tmp_path


def test_persist_writes_json_file(tmp_runs, monkeypatch):
    """test_plan.json written under runs/<ticket>/."""
    captured: list[dict] = []
    monkeypatch.setattr(learner, "add_attachment",
                        lambda **kw: captured.append(kw) or True)
    monkeypatch.setattr(learner, "record_audit",
                        lambda **kw: None)

    test_plan = {
        "artifact_type": "test_plan",
        "tests": [
            {"name": "t_a", "target_class": "A", "target_method": "m",
             "scenario": "happy", "expected": "ok",
             "framework": "pytest"},
        ],
        "coverage_target": 0.9,
    }
    log = _NoopLogger()
    out = rt._persist_tester_specs(
        ticket_id="T-PERSIST", test_plan=test_plan, log=log,
    )
    assert out
    p = Path(out)
    assert p.is_file()
    body = json.loads(p.read_text())
    assert body["tests"][0]["name"] == "t_a"
    # add_attachment got role=tester_specs
    assert captured and captured[0]["role"] == "tester_specs"
    assert captured[0]["filename"] == "test_plan.json"
    assert captured[0]["bytes_"] == p.stat().st_size


def test_persist_skips_empty_plan(tmp_runs, monkeypatch):
    monkeypatch.setattr(learner, "add_attachment", lambda **kw: True)
    monkeypatch.setattr(learner, "record_audit", lambda **kw: None)

    log = _NoopLogger()
    out = rt._persist_tester_specs(
        ticket_id="T-EMPTY", test_plan={}, log=log,
    )
    assert out == ""


def test_persist_handles_zero_tests(tmp_runs, monkeypatch):
    """Empty tests[] still writes the file (audit value)."""
    monkeypatch.setattr(learner, "add_attachment", lambda **kw: True)
    monkeypatch.setattr(learner, "record_audit", lambda **kw: None)

    log = _NoopLogger()
    out = rt._persist_tester_specs(
        ticket_id="T-ZERO",
        test_plan={"tests": [], "coverage_target": 0.5},
        log=log,
    )
    assert out
    body = json.loads(Path(out).read_text())
    assert body["tests"] == []


def test_persist_swallows_attachment_error(tmp_runs, monkeypatch):
    """DB error in add_attachment must NOT raise from the persister."""
    def boom(**kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(learner, "add_attachment", boom)
    monkeypatch.setattr(learner, "record_audit", lambda **kw: None)

    log = _NoopLogger()
    out = rt._persist_tester_specs(
        ticket_id="T-DB-DOWN",
        test_plan={"tests": [{"name": "x"}]},
        log=log,
    )
    # Disk write succeeded → returns path; attachment failure was logged.
    assert out
    assert Path(out).is_file()
