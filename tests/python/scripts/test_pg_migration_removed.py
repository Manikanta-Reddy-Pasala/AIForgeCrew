"""The Postgres→SQLite migration is GONE, and an un-migrated Postgres is safe.

It had been broken since `_PgChatStore` went away with Postgres support:
converge ran `scripts/migrate_to_sqlite.py` as a subprocess, that script failed
on its first import, and converge retried the identical failure on every boot.
Reviving it would need a psycopg dependency this build does not ship, and the
upgrade is not needed — so the path was removed.

The property that matters now is DESTRUCTIVE-SAFETY: converge must never
delete a Postgres whose data was never migrated.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from aiforge_core.deploy import converge as cv

_REPO = Path(__file__).resolve().parents[3]


class _Docker:
    def __init__(self, containers=(), ok=True):
        self.containers = set(containers)
        self.ok = ok
        self.calls: list[tuple] = []

    def __call__(self, *args, timeout=120):
        self.calls.append(args)
        if args[0] == "info":
            return (0, "") if self.ok else (1, "")
        if args[0] == "ps":
            name = args[-1].replace("name=", "").strip("^$")
            return (0, "cid" if name in self.containers else "")
        return (0, "")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_AUTO_MIGRATE", "1")
    monkeypatch.setattr(cv, "_repo_root", lambda: tmp_path / "repo")
    (tmp_path / "repo").mkdir(parents=True, exist_ok=True)
    (tmp_path / "cfg").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cv, "_SUDO", [], raising=False)
    return tmp_path


# ── the machinery is gone ─────────────────────────────────────────────


def test_the_migration_script_is_deleted():
    assert not (_REPO / "scripts" / "migrate_to_sqlite.py").exists()


def test_converge_no_longer_carries_migration_machinery():
    for name in ("_migrate_pg_to_sqlite", "_pg_url"):
        assert not hasattr(cv, name), f"{name} came back"
    assert "migrate_to_sqlite.py" not in inspect.getsource(cv.converge)


def test_the_postgres_chat_store_is_still_gone():
    from aiforge_core.runtime import chat_store
    assert not hasattr(chat_store, "_PgChatStore")
    assert hasattr(chat_store, "_SqliteChatStore")


# ── THE safety property ───────────────────────────────────────────────


def test_an_unmigrated_postgres_is_left_completely_alone(env, monkeypatch):
    """Its container and volumes are the only copy of that data."""
    d = _Docker(containers={"aiforge-postgres"}, ok=True)
    monkeypatch.setattr(cv, "_docker", d)
    monkeypatch.setattr(cv, "_remove_db_infra",
                        lambda: pytest.fail("must not remove an un-migrated Postgres"))

    out = cv.converge()
    assert "not removed" in out["skipped"]
    assert not any(a[0] in ("rm", "volume", "image") for a in d.calls), \
        "no destructive docker call may be made"


def test_it_does_not_mark_done_while_postgres_is_still_there(env, monkeypatch):
    """Writing the marker would let the already-migrated branch delete these
    containers on the very next boot — the exact data loss to avoid."""
    monkeypatch.setattr(cv, "_docker", _Docker(containers={"aiforge-postgres"}))
    monkeypatch.setattr(cv, "_remove_db_infra", lambda: {})
    cv.converge()
    assert not cv._marker().exists()


def test_it_says_so_rather_than_failing_silently(env, monkeypatch, caplog):
    # `aiforge` is configured with propagate=False the moment anything imports
    # the API or the structured logger, so in a full-suite run caplog's root
    # handler sees NOTHING and this test passed only when run alone. Restore
    # propagation for the duration rather than depending on import order.
    import logging
    monkeypatch.setattr(logging.getLogger("aiforge"), "propagate", True)
    monkeypatch.setattr(cv, "_docker", _Docker(containers={"aiforge-postgres"}))
    with caplog.at_level("WARNING"):
        cv.converge()
    msg = " ".join(r.message for r in caplog.records)
    assert "no longer migrates" in msg
    assert "docker rm -f aiforge-postgres" in msg, "tell the operator what to do"


# ── the cleanup that IS still wanted ──────────────────────────────────


def test_an_already_migrated_box_still_gets_leftovers_removed(env, monkeypatch):
    """The marker proves the data moved, so leftovers are safe to remove."""
    cv._mark_done()
    monkeypatch.setattr(cv, "_docker", _Docker(containers={"aiforge-neo4j"}))
    monkeypatch.setattr(cv, "_remove_db_infra",
                        lambda: {"containers": ["aiforge-neo4j"],
                                 "volumes": [], "images": []})
    out = cv.converge()
    assert out["ok"] is True
    assert out["cleaned"]["containers"] == ["aiforge-neo4j"]


def test_a_clean_box_marks_done_and_skips(env, monkeypatch):
    monkeypatch.setattr(cv, "_docker", _Docker(containers=()))
    assert cv.converge() == {"skipped": "no aiforge-postgres"}
    assert cv._marker().exists()
