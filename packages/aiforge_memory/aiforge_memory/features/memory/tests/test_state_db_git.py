"""L8 — sqlite git_state + per-file hash round-trips."""
from __future__ import annotations

from pathlib import Path

import pytest

from aiforge_memory.core import state as sdb


@pytest.fixture()
def db(tmp_path: Path):
    conn = sdb.open_db(tmp_path / "codemem.state.db")
    sdb.migrate(conn)
    yield conn
    conn.close()


def test_git_state_round_trip(db) -> None:
    sdb.set_repo_git_head(db, repo="r1", head_sha="abc", branch="main")
    out = sdb.get_repo_git_head(db, repo="r1")
    assert out == ("abc", "main")


def test_git_state_overwrites(db) -> None:
    sdb.set_repo_git_head(db, repo="r1", head_sha="v1", branch="main")
    sdb.set_repo_git_head(db, repo="r1", head_sha="v2", branch="dev")
    assert sdb.get_repo_git_head(db, repo="r1") == ("v2", "dev")


def test_git_state_missing_returns_none(db) -> None:
    assert sdb.get_repo_git_head(db, repo="missing") is None


def test_file_hashes_bulk(db) -> None:
    sdb.upsert_file_hashes(
        db, repo="r1",
        hashes={"a.py": "h1", "b.py": "h2"},
    )
    got = sdb.get_file_hashes(db, repo="r1")
    assert got == {"a.py": "h1", "b.py": "h2"}


def test_file_hashes_update_in_place(db) -> None:
    sdb.upsert_file_hashes(db, repo="r1", hashes={"a.py": "h1"})
    sdb.upsert_file_hashes(db, repo="r1", hashes={"a.py": "h2"})
    assert sdb.get_file_hashes(db, repo="r1") == {"a.py": "h2"}


def test_migrate_includes_git_state(db) -> None:
    cur = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='git_state'"
    )
    assert cur.fetchone() is not None
