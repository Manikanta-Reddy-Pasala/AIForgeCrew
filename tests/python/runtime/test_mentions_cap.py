"""Fix M2 — @-mentions capped by COUNT and aggregate size.

Each ``@file`` is capped at 12K chars but the loop used to resolve EVERY
mention — 26 ``@file`` → ~312K chars. Now capped at ``AIFORGE_MENTIONS_MAX``
files + ``AIFORGE_MENTIONS_TOTAL_CHARS`` aggregate.
"""
from __future__ import annotations

import pytest

from aiforge_core.runtime import mentions as mn


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.delenv("AIFORGE_MENTIONS_MAX", raising=False)
    monkeypatch.delenv("AIFORGE_MENTIONS_TOTAL_CHARS", raising=False)


def _make_files(root, n):
    for i in range(n):
        (root / f"f{i:02d}.txt").write_text(f"content of file {i}\n")


def test_mentions_capped_by_count(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_WORKSPACE_DIR", str(tmp_path))
    _make_files(tmp_path, 20)
    text = " ".join(f"@f{i:02d}.txt" for i in range(20))
    block, resolved = mn.expand(text, str(tmp_path))
    # At most 8 file blocks resolved (default cap).
    assert block.count("(file):") <= 8
    assert len(resolved) <= 8
    assert "more mentions omitted" in block


def test_mentions_max_env_tunes(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_MENTIONS_MAX", "3")
    _make_files(tmp_path, 10)
    text = " ".join(f"@f{i:02d}.txt" for i in range(10))
    block, resolved = mn.expand(text, str(tmp_path))
    assert len(resolved) == 3
    assert "7 more mentions omitted" in block


def test_mentions_aggregate_char_cap(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_MENTIONS_MAX", "100")     # count won't bind
    monkeypatch.setenv("AIFORGE_MENTIONS_TOTAL_CHARS", "3000")
    # 10 files of ~1000 chars each = ~10K raw, cap at 3000.
    for i in range(10):
        (tmp_path / f"big{i}.txt").write_text("x" * 1000)
    text = " ".join(f"@big{i}.txt" for i in range(10))
    block, resolved = mn.expand(text, str(tmp_path))
    assert len(block) <= 3000 + 300      # aggregate cap (+ header/note slack)
    assert "more mentions omitted" in block


def test_small_mentions_unchanged(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_WORKSPACE_DIR", str(tmp_path))
    (tmp_path / "a.txt").write_text("alpha")
    block, resolved = mn.expand("look at @a.txt please", str(tmp_path))
    assert "alpha" in block
    assert "omitted" not in block
    assert resolved == ["a.txt"]
