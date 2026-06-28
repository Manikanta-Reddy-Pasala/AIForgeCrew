"""L12 — scheduler config + lockfile + git poll-decide (no daemon spawn)."""
from __future__ import annotations

import os
import subprocess

from aiforge_memory.features.scheduler import runner as scheduler

# ─── Config persistence ───────────────────────────────────────────────

def test_config_round_trip(tmp_path):
    cfg_path = tmp_path / "scheduler.yaml"
    rs = scheduler.RepoSchedule(
        name="r1", path="/tmp/r1", interval_seconds=300,
    )
    cfg = scheduler.SchedulerConfig(repos=[rs])
    cfg.save(cfg_path)
    loaded = scheduler.SchedulerConfig.load(cfg_path)
    assert len(loaded.repos) == 1
    assert loaded.repos[0].name == "r1"
    assert loaded.repos[0].interval_seconds == 300


def test_add_repo_dedupes_by_name(tmp_path):
    cfg_path = tmp_path / "scheduler.yaml"
    scheduler.add_repo(
        scheduler.RepoSchedule(name="r1", path="/a", interval_seconds=60),
        path=cfg_path,
    )
    scheduler.add_repo(
        scheduler.RepoSchedule(name="r1", path="/b", interval_seconds=120),
        path=cfg_path,
    )
    cfg = scheduler.SchedulerConfig.load(cfg_path)
    assert len(cfg.repos) == 1
    assert cfg.repos[0].path == "/b"
    assert cfg.repos[0].interval_seconds == 120


def test_remove_repo(tmp_path):
    cfg_path = tmp_path / "scheduler.yaml"
    scheduler.add_repo(
        scheduler.RepoSchedule(name="r1", path="/a"),
        path=cfg_path,
    )
    assert scheduler.remove_repo("r1", path=cfg_path) is True
    assert scheduler.remove_repo("r1", path=cfg_path) is False


def test_load_missing_file_returns_empty(tmp_path):
    cfg = scheduler.SchedulerConfig.load(tmp_path / "missing.yaml")
    assert cfg.repos == []


def test_load_malformed_yaml_returns_empty(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("not: : yaml :")
    cfg = scheduler.SchedulerConfig.load(p)
    assert cfg.repos == []


# ─── Lockfile ─────────────────────────────────────────────────────────

def test_lock_acquire_release(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(os.path, "expanduser",
                        lambda p: p.replace("~", str(tmp_path)))
    assert scheduler._acquire_lock("repo1") is True
    assert scheduler._acquire_lock("repo1") is False  # held by us
    scheduler._release_lock("repo1")
    assert scheduler._acquire_lock("repo1") is True


def test_lock_reclaims_stale(tmp_path, monkeypatch):
    monkeypatch.setattr(os.path, "expanduser",
                        lambda p: p.replace("~", str(tmp_path)))
    lf = scheduler._lockfile("repoX")
    lf.parent.mkdir(parents=True, exist_ok=True)
    lf.write_text("99999999")           # pid that doesn't exist
    assert scheduler._acquire_lock("repoX") is True
    scheduler._release_lock("repoX")


# ─── git poll decision ────────────────────────────────────────────────

class _FakeProc:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode


def test_fetch_and_pull_skipped_when_dirty(tmp_path, monkeypatch):
    seq = [
        _FakeProc(returncode=0),       # fetch
        _FakeProc(stdout="3\n"),       # behind=3
        _FakeProc(stdout=" M file"),   # dirty
    ]
    def fake_run(*a, **kw):
        return seq.pop(0)
    monkeypatch.setattr(subprocess, "run", fake_run)

    out = scheduler.fetch_and_maybe_pull(tmp_path, do_pull=True)
    assert out.fetched is True
    assert out.behind == 3
    assert out.pulled is False
    assert out.skipped_reason == "dirty"


def test_fetch_and_pull_proceeds_when_clean(tmp_path, monkeypatch):
    seq = [
        _FakeProc(returncode=0),       # fetch
        _FakeProc(stdout="2\n"),       # behind
        _FakeProc(stdout=""),          # clean
        _FakeProc(stdout="origin/main\n"),  # has upstream
        _FakeProc(returncode=0),       # pull --ff-only
    ]
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: seq.pop(0))

    out = scheduler.fetch_and_maybe_pull(tmp_path, do_pull=True)
    assert out.fetched and out.pulled
    assert out.behind == 2


def test_fetch_only_when_up_to_date(tmp_path, monkeypatch):
    seq = [
        _FakeProc(returncode=0),       # fetch
        _FakeProc(stdout="0\n"),       # behind=0
    ]
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: seq.pop(0))
    out = scheduler.fetch_and_maybe_pull(tmp_path)
    assert out.fetched is True
    assert out.pulled is False
    assert out.behind == 0


def test_fetch_failure_returns_clean_outcome(tmp_path, monkeypatch):
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: _FakeProc(returncode=1),
    )
    out = scheduler.fetch_and_maybe_pull(tmp_path)
    assert out.fetched is False and out.pulled is False


# ─── Dynamic timeout (per_file_seconds scaling) ───────────────────────

def test_count_ingest_files_walks_extensions(tmp_path):
    (tmp_path / "a.py").write_text("x")
    (tmp_path / "b.js").write_text("x")
    (tmp_path / "c.bin").write_text("x")
    (tmp_path / "d.lock").write_text("x")
    nm = tmp_path / "node_modules"; nm.mkdir()
    (nm / "vendor.js").write_text("x")
    n = scheduler._count_ingest_files(str(tmp_path))
    assert n == 2  # a.py + b.js; vendored + binary excluded


def test_count_ingest_files_handles_empty():
    assert scheduler._count_ingest_files("") == 0
    assert scheduler._count_ingest_files("/no/such/path") == 0


def test_effective_timeout_disabled_returns_floor(tmp_path):
    rs = scheduler.RepoSchedule(name="r", path=str(tmp_path),
                                timeout_seconds=900,
                                per_file_seconds=0.0)
    t, n = scheduler._effective_timeout(rs, str(tmp_path))
    assert t == 900
    assert n == 0  # not counted when disabled


def test_effective_timeout_scales_with_files(tmp_path):
    for i in range(50):
        (tmp_path / f"f{i}.py").write_text("x")
    rs = scheduler.RepoSchedule(name="r", path=str(tmp_path),
                                timeout_seconds=300,
                                per_file_seconds=2.0)
    t, n = scheduler._effective_timeout(rs, str(tmp_path))
    assert n == 50
    assert t == 300  # 50 × 2 = 100s, floor 300 wins


def test_effective_timeout_scales_above_floor(tmp_path):
    for i in range(500):
        (tmp_path / f"f{i}.py").write_text("x")
    rs = scheduler.RepoSchedule(name="r", path=str(tmp_path),
                                timeout_seconds=300,
                                per_file_seconds=3.0)
    t, n = scheduler._effective_timeout(rs, str(tmp_path))
    assert n == 500
    assert t == 1500  # 500 × 3 = 1500, exceeds floor


def test_effective_timeout_capped_by_env(tmp_path, monkeypatch):
    for i in range(2000):
        (tmp_path / f"f{i}.py").write_text("x")
    monkeypatch.setenv("AIFORGE_SCHEDULER_MAX_TIMEOUT_S", "1000")
    rs = scheduler.RepoSchedule(name="r", path=str(tmp_path),
                                timeout_seconds=300,
                                per_file_seconds=10.0)
    t, _ = scheduler._effective_timeout(rs, str(tmp_path))
    assert t == 1000  # 2000 × 10 = 20000, capped at 1000


def test_config_round_trip_includes_per_file(tmp_path):
    cfg_path = tmp_path / "scheduler.yaml"
    rs = scheduler.RepoSchedule(name="r1", path="/tmp/r1",
                                timeout_seconds=300,
                                per_file_seconds=1.5)
    cfg = scheduler.SchedulerConfig(repos=[rs])
    cfg.save(cfg_path)
    loaded = scheduler.SchedulerConfig.load(cfg_path)
    assert loaded.repos[0].per_file_seconds == 1.5
