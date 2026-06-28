"""L1 — sqlite state DB: open/migrate/round-trip for merkle_repo + service_overrides."""
from __future__ import annotations

from pathlib import Path

import pytest

from aiforge_memory.core import state as sdb


@pytest.fixture()
def db(tmp_path: Path):
    path = tmp_path / "codemem.state.db"
    conn = sdb.open_db(path)
    sdb.migrate(conn)
    yield conn
    conn.close()


def test_migrate_creates_tables(db) -> None:
    cur = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    names = [r[0] for r in cur.fetchall()]
    assert "merkle_files" in names
    assert "merkle_repo" in names
    assert "service_overrides" in names
    # query_cache was dead surface (never read/written) — removed.
    assert "query_cache" not in names


def test_repo_hash_round_trip(db) -> None:
    sdb.set_repo_pack_sha(db, repo="PosClientBackend", pack_sha="abc123")
    sha = sdb.get_repo_pack_sha(db, repo="PosClientBackend")
    assert sha == "abc123"


def test_repo_hash_missing_returns_none(db) -> None:
    assert sdb.get_repo_pack_sha(db, repo="UnknownRepo") is None


def test_repo_hash_overwrites(db) -> None:
    sdb.set_repo_pack_sha(db, repo="X", pack_sha="v1")
    sdb.set_repo_pack_sha(db, repo="X", pack_sha="v2")
    assert sdb.get_repo_pack_sha(db, repo="X") == "v2"


def test_idempotent_migrate(db) -> None:
    # second migrate must not raise
    sdb.migrate(db)
    sdb.migrate(db)
