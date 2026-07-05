"""`_cached_find_by_source` — the per-turn repo/rules context lookup used to
glob + parse EVERY file in the memory dir on every single chat message with
no caching. This wraps `md_store._find_by_source` with a positive-only cache
(never caches a miss — a source can appear moments later in the same turn
via rule capture) keyed by (memory_dir, source) so a changed
AIFORGE_MEMORY_MD_DIR can never serve a stale path.
"""
from __future__ import annotations

import pytest

from aiforge_core.runtime import chat_agent as ca


@pytest.fixture(autouse=True)
def _clear_cache():
    ca._source_path_cache.clear()
    yield
    ca._source_path_cache.clear()


def test_hit_is_cached_second_call_skips_scan(tmp_path, monkeypatch):
    from aiforge_core.memory import md_store
    monkeypatch.setattr(md_store, "memory_dir", lambda: tmp_path)
    p = tmp_path / "rules.md"
    p.write_text("---\nsource: rules:global\n---\n\n- x\n")

    calls = []
    real_find = md_store._find_by_source

    def counting_find(source):
        calls.append(source)
        return real_find(source)

    monkeypatch.setattr(md_store, "_find_by_source", counting_find)

    first = ca._cached_find_by_source("rules:global")
    second = ca._cached_find_by_source("rules:global")
    assert first == second == p
    assert calls == ["rules:global"]   # second call never re-scanned


def test_miss_is_never_cached(tmp_path, monkeypatch):
    from aiforge_core.memory import md_store
    monkeypatch.setattr(md_store, "memory_dir", lambda: tmp_path)

    calls = []

    def fake_find(source):
        calls.append(source)
        return None

    monkeypatch.setattr(md_store, "_find_by_source", fake_find)

    assert ca._cached_find_by_source("rules:global") is None
    assert ca._cached_find_by_source("rules:global") is None
    assert calls == ["rules:global", "rules:global"]   # re-checked both times


def test_memory_dir_change_busts_cache(tmp_path, monkeypatch):
    from aiforge_core.memory import md_store
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    (dir_a / "rules.md").write_text("---\nsource: rules:global\n---\n\n- from a\n")
    (dir_b / "rules.md").write_text("---\nsource: rules:global\n---\n\n- from b\n")

    current = {"dir": dir_a}
    monkeypatch.setattr(md_store, "memory_dir", lambda: current["dir"])

    got_a = ca._cached_find_by_source("rules:global")
    assert got_a == dir_a / "rules.md"

    current["dir"] = dir_b
    got_b = ca._cached_find_by_source("rules:global")
    assert got_b == dir_b / "rules.md"   # NOT the stale dir_a path
