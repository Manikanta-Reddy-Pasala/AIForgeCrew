"""The SQLite convergence: neutralise stale PG/Neo4j env, and tear down the
old dockerized DB infra WITHOUT touching langfuse.

The module was at 0% coverage while being the thing that keeps a removed
database from being re-pointed at on every boot. Docker is stubbed at the one
seam (`_docker`) so the decision logic is exercised for real.
"""
from __future__ import annotations

import pytest

from aiforge_core.deploy import converge as cv


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_AUTO_MIGRATE", "1")
    monkeypatch.setattr(cv, "_repo_root", lambda: tmp_path / "repo")
    (tmp_path / "repo").mkdir(parents=True, exist_ok=True)
    (tmp_path / "cfg").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cv, "_SUDO", [], raising=False)
    return tmp_path


class _Docker:
    """Scripted docker: maps an argv prefix to (rc, stdout)."""

    def __init__(self, containers=(), ok=True):
        self.containers = set(containers)
        self.ok = ok
        self.calls: list[tuple] = []

    def __call__(self, *args, timeout=120):
        self.calls.append(args)
        if args[0] == "info":
            return (0, "") if self.ok else (1, "")
        if args[0] == "ps":
            # real call: _docker("ps", "-aq", "-f", "name=^<name>$")
            name = args[-1].replace("name=", "").strip("^$")
            return (0, "cid" if name in self.containers else "")
        if args[0] in ("rm", "stop", "start", "volume", "image", "images"):
            return (0, "")
        return (0, "")


# ── env neutralisation ────────────────────────────────────────────────


def test_neutralise_comments_out_every_stale_backend_key(env, tmp_path):
    f = tmp_path / "x.env"
    f.write_text(
        "AIFORGE_PG_URL=postgres://x\n"
        "NEO4J_URI=bolt://y\n"
        "AIFORGE_MEMORY_BACKEND=neo4j\n"
        "KEEP_ME=1\n")
    assert cv._neutralise_db_lines(f) is True
    out = f.read_text()
    assert out.count("# [converge→sqlite]") == 3
    assert "KEEP_ME=1" in out, "unrelated keys must survive untouched"


def test_neutralise_is_idempotent(env, tmp_path):
    f = tmp_path / "x.env"
    f.write_text("AIFORGE_PG_URL=postgres://x\n")
    assert cv._neutralise_db_lines(f) is True
    assert cv._neutralise_db_lines(f) is False, "already-commented lines are left alone"


def test_neutralise_skips_a_missing_file(env, tmp_path):
    assert cv._neutralise_db_lines(tmp_path / "nope.env") is False


def test_neutralise_survives_an_unreadable_file(env, tmp_path, monkeypatch):
    f = tmp_path / "x.env"
    f.write_text("AIFORGE_PG_URL=1\n")
    monkeypatch.setattr(type(f), "read_text",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
    assert cv._neutralise_db_lines(f) is False, "best-effort, never raises"


def test_clear_pg_from_env_covers_repo_and_runtime_env(env, tmp_path):
    (tmp_path / "repo" / ".env").write_text("AIFORGE_PG_URL=a\n")
    (tmp_path / "repo" / "aiforge.env").write_text("NEO4J_URI=b\n")
    (tmp_path / "cfg" / "runtime.env").write_text("AIFORGE_FORCE_PG=1\n")
    cv._clear_pg_from_env()
    for p in ((tmp_path / "repo" / ".env"), (tmp_path / "repo" / "aiforge.env"),
              (tmp_path / "cfg" / "runtime.env")):
        assert "# [converge→sqlite]" in p.read_text(), p


# ── converge decision paths ───────────────────────────────────────────


def test_disabled_by_env_short_circuits(env, monkeypatch):
    monkeypatch.setenv("AIFORGE_AUTO_MIGRATE", "0")
    assert cv.converge() == {"skipped": "disabled"}


def test_no_docker_marks_done_and_skips(env, monkeypatch):
    monkeypatch.setattr(cv, "_docker", _Docker(ok=False))
    assert cv.converge() == {"skipped": "no docker"}
    assert cv._marker().exists(), "nothing to converge → don't retry every boot"


def test_no_prior_postgres_marks_done_and_skips(env, monkeypatch):
    monkeypatch.setattr(cv, "_docker", _Docker(containers=(), ok=True))
    assert cv.converge() == {"skipped": "no aiforge-postgres"}
    assert cv._marker().exists()


def test_already_done_skips_when_nothing_lingers(env, monkeypatch):
    cv._mark_done()
    monkeypatch.setattr(cv, "_docker", _Docker(containers=(), ok=True))
    assert cv.converge() == {"skipped": "already done"}


def test_already_done_still_removes_lingering_infra(env, monkeypatch):
    """The marker proves the data moved, so leftover containers are safe to
    remove — and must be, or they linger forever."""
    cv._mark_done()
    d = _Docker(containers={"aiforge-neo4j"}, ok=True)
    monkeypatch.setattr(cv, "_docker", d)
    monkeypatch.setattr(cv, "_remove_db_infra",
                        lambda: {"containers": ["aiforge-neo4j"],
                                 "volumes": [], "images": []})
    out = cv.converge()
    assert out["ok"] is True
    assert out["cleaned"]["containers"] == ["aiforge-neo4j"]


def test_langfuse_is_never_in_the_infra_removal_set():
    """The one thing this teardown must never touch."""
    assert not any("langfuse" in n for n in cv._INFRA)


# ── docker helpers ────────────────────────────────────────────────────


def test_docker_reports_127_when_not_installed(env, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda n: None)
    assert cv._docker("info") == (127, "")


def test_container_exists_reads_the_id_output(env, monkeypatch):
    monkeypatch.setattr(cv, "_docker", _Docker(containers={"aiforge-postgres"}))
    assert cv._container_exists("aiforge-postgres") is True
    assert cv._container_exists("nope") is False
