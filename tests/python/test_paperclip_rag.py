from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("chromadb")  # skip entire module if chromadb not installed

from aiforge_core.rag import RagIndex


def test_index_and_query(tmp_path: Path) -> None:
    repo = tmp_path
    (repo / "docs").mkdir()
    (repo / "docs" / "auth.md").write_text("# Auth\n\nWe use JWT with 15 minute expiry.")
    (repo / "docs" / "db.md").write_text("# Database\n\nPostgres 16, multi-tenant schema.")
    (repo / "README.md").write_text("# Project")
    idx = RagIndex(repo, db_dir=tmp_path / "rag-db")
    stats = idx.reindex(sources=["docs/**/*.md", "README.md"])
    assert stats["files"] >= 3
    assert stats["chunks"] >= 3

    hits = idx.query("how long does the token live?", top_k=2)
    assert hits
    # Expect the auth doc (JWT expiry) to surface as the top hit.
    assert any("JWT" in h.text or "auth" in h.source for h in hits)
