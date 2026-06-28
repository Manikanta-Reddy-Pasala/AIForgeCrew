"""The merkle-skip plumbing for failed files is retained even though
chunk embedding was removed (chunk_and_embed's failed_paths is now
always empty) — a future failure source can reuse it. This pins the
delta behaviour: a path reported failed keeps its stale hash so the
next merkle diff retries it."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from aiforge_memory.core import state as sdb
from aiforge_memory.features.delta import extract as delta


def test_delta_skips_merkle_update_for_failed_files(tmp_path) -> None:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / "a.py").write_text("def a(): pass\n")
    (repo_dir / "b.py").write_text("def b(): pass\n")

    state = sdb.open_db(tmp_path / "state.db")
    sdb.migrate(state)
    sdb.upsert_file_hashes(state, repo="t",
                           hashes={"a.py": "old-a", "b.py": "old-b"})

    driver = MagicMock()
    with patch("aiforge_memory.features.delta.extract.embed"
               ".chunk_and_embed", return_value=([], ["b.py"])), \
         patch("aiforge_memory.features.delta.extract.file_summary"
               ".summarize_files", return_value=[]):
        delta.ingest_delta(
            repo_name="t", repo_path=repo_dir,
            driver=driver, state_conn=state,
        )

    hashes = sdb.get_file_hashes(state, repo="t")
    assert hashes["a.py"] != "old-a", "healthy file hash must update"
    assert hashes["b.py"] == "old-b", "failed file keeps stale hash → retried"
