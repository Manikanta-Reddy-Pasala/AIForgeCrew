"""K2 — memory decay: archive stale never-reused facts.

Fake-driver unit level: asserts the Cypher rule shape, batching loop and
the scheduler/CLI wiring — no live Neo4j.
"""
from __future__ import annotations

from aiforge_memory.features.memory import decay


class _FakeResult:
    def __init__(self, n):
        self._n = n

    def single(self):
        return {"n": self._n}


class _FakeSession:
    def __init__(self, driver):
        self._driver = driver

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def run(self, cypher, **params):
        self._driver.calls.append((cypher, params))
        return _FakeResult(self._driver.batch_results.pop(0)
                           if self._driver.batch_results else 0)


class _FakeDriver:
    def __init__(self, batch_results=None):
        self.calls: list[tuple[str, dict]] = []
        self.batch_results = list(batch_results or [])

    def session(self):
        return _FakeSession(self)


def test_decay_cypher_archives_only_cold_active_old_facts():
    cy = decay._DECAY_CY
    # Observations only — Decision_v2 has no touch path, so age-based
    # decay would archive every decision older than the threshold.
    assert "MATCH (o:Observation_v2)" in cy
    assert "Decision_v2" not in cy
    assert "o.status IS NULL OR o.status = 'active'" in cy
    assert "coalesce(o.seen_count, 1) <= 1" in cy
    assert "duration({days: $days})" in cy
    assert "o.last_seen_at IS NULL" in cy           # recent bump survives
    assert "SET o.status = 'archived'" in cy        # archive, not delete
    assert "DELETE" not in cy


def test_run_decay_single_partial_batch(monkeypatch):
    monkeypatch.setenv("AIFORGE_DECAY_BATCH", "500")
    drv = _FakeDriver(batch_results=[37])
    res = decay.run_decay(drv, max_age_days=45)
    assert res == {"archived": 37, "max_age_days": 45}
    assert len(drv.calls) == 1
    assert drv.calls[0][1] == {"days": 45, "batch": 500}


def test_run_decay_loops_until_drained(monkeypatch):
    monkeypatch.setenv("AIFORGE_DECAY_BATCH", "500")
    drv = _FakeDriver(batch_results=[500, 500, 12])
    res = decay.run_decay(drv)
    assert res["archived"] == 1012
    assert len(drv.calls) == 3


def test_scheduler_loop_runs_decay_once_per_interval(tmp_path, monkeypatch):
    """run_loop(once=True) must fire decay on its sweep."""
    from aiforge_memory.features.scheduler import runner as scheduler

    monkeypatch.setattr(scheduler, "STATUS_PATH",
                        tmp_path / "status.json")
    monkeypatch.setenv("AIFORGE_DECAY_INTERVAL_S", "86400")
    calls = []
    monkeypatch.setattr(decay, "run_decay",
                        lambda drv, max_age_days=30:
                        calls.append(max_age_days) or
                        {"archived": 0, "max_age_days": max_age_days})

    cfg = scheduler.SchedulerConfig(repos=[scheduler.RepoSchedule(
        name="r1", path=str(tmp_path), interval_seconds=600,
    )])
    monkeypatch.setattr(
        scheduler, "tick_repo",
        lambda rs, **kw: scheduler.RepoStatus(name=rs.name,
                                              last_status="no_changes"),
    )
    scheduler.run_loop(
        config=cfg,
        driver_factory=lambda: type("D", (), {"close": lambda self: None})(),
        state_factory=lambda: None,
        log_path=tmp_path / "log.txt",
        once=True,
    )
    assert calls == [30]


def test_cli_decay_command_registered():
    from aiforge_memory.api.commands import COMMAND_MODULES
    names = [m.__name__.rsplit(".", 1)[-1] for m in COMMAND_MODULES]
    assert "decay" in names
