"""Unit tests for AiForgeMemory ContextBundle integration in
unified_query (source #7 = afm_bundle). No live Neo4j / sidecars."""
from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest


@pytest.fixture
def uq():
    """Fresh import per test + cleanup so test_doer_tools' sys.modules
    monkeypatch isolation isn't broken by this file caching the module
    (parent-package attribute lookup wins over sys.modules.setitem)."""
    sys.modules.pop("aiforge_core.memory.unified_query", None)
    import aiforge_core.memory as _mem
    if hasattr(_mem, "unified_query"):
        delattr(_mem, "unified_query")
    from aiforge_core.memory import unified_query
    yield unified_query
    sys.modules.pop("aiforge_core.memory.unified_query", None)
    if hasattr(_mem, "unified_query"):
        delattr(_mem, "unified_query")


def _make_bundle(**kwargs) -> SimpleNamespace:
    """Mimic ContextBundle structure used by _afm_bundle."""
    base = {
        "repo_map": "",
        "conventions_md": "",
        "chunks": [],
        "notes": [],
        "docs": [],
        "observations": [],
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_afm_bundle_emits_repo_map_row(uq) -> None:
    bundle = _make_bundle(repo_map="api/main.py:\n  - process\n")
    with patch("aiforge_memory.api.read.context_bundle_object",
               return_value=bundle, create=True):
        rows = uq._afm_bundle("fix payment", repo="X", role="doer")
    assert any("[afm/repo_map]" in r["text"] for r in rows)
    assert rows[0]["score"] == 0.95
    assert rows[0]["source_uri"] == "afm://X/repo_map"


def test_afm_bundle_emits_conventions_row(uq) -> None:
    bundle = _make_bundle(conventions_md="- use ruff\n- pin deps\n")
    with patch("aiforge_memory.api.read.context_bundle_object",
               return_value=bundle, create=True):
        rows = uq._afm_bundle("any", repo="X", role="doer")
    assert any("[afm/conventions]" in r["text"] for r in rows)
    assert any("use ruff" in r["text"] for r in rows)


def test_afm_bundle_caps_chunks_at_5(uq) -> None:
    chunks = [
        {"file_path": f"f{i}.py", "text": f"chunk{i}"} for i in range(10)
    ]
    bundle = _make_bundle(chunks=chunks)
    with patch("aiforge_memory.api.read.context_bundle_object",
               return_value=bundle, create=True):
        rows = uq._afm_bundle("any", repo="X", role="doer")
    chunk_rows = [r for r in rows if "[afm/chunk" in r["text"]]
    assert len(chunk_rows) == 5


def test_afm_bundle_skips_chunks_missing_path_or_body(uq) -> None:
    bundle = _make_bundle(chunks=[
        {"file_path": "", "text": "no path"},
        {"file_path": "ok.py", "text": ""},
        {"file_path": "good.py", "text": "real"},
    ])
    with patch("aiforge_memory.api.read.context_bundle_object",
               return_value=bundle, create=True):
        rows = uq._afm_bundle("any", repo="X", role="doer")
    assert any("good.py" in r["text"] for r in rows)
    assert not any("no path" in r["text"] for r in rows)


def test_afm_bundle_emits_notes_and_docs(uq) -> None:
    bundle = _make_bundle(
        notes=[{"id": "n1", "title": "Migration", "body": "v2 reindex needed"}],
        docs=[{"id": "d1", "title": "Vector",
               "url": "https://x/y", "body": "cosine"}],
    )
    with patch("aiforge_memory.api.read.context_bundle_object",
               return_value=bundle, create=True):
        rows = uq._afm_bundle("any", repo="X", role="doer")
    assert any("[afm/note Migration]" in r["text"] for r in rows)
    assert any("[afm/doc Vector]" in r["text"] for r in rows)
    doc_row = next(r for r in rows if "Vector" in r["text"])
    assert doc_row["source_uri"] == "https://x/y"


def test_afm_bundle_emits_observations_with_kind(uq) -> None:
    bundle = _make_bundle(observations=[
        {"id": "o1", "kind": "lesson", "text": "use cl100k_base", "score": 0.8},
    ])
    with patch("aiforge_memory.api.read.context_bundle_object",
               return_value=bundle, create=True):
        rows = uq._afm_bundle("any", repo="X", role="doer")
    obs_row = next(r for r in rows if "[afm/lesson]" in r["text"])
    assert obs_row["score"] == 0.8


def test_afm_bundle_returns_empty_on_none_bundle(uq) -> None:
    with patch("aiforge_memory.api.read.context_bundle_object",
               return_value=None):
        rows = uq._afm_bundle("any", repo="X", role="doer")
    assert rows == []


def test_afm_bundle_returns_empty_on_import_failure(
    uq, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If aiforge_memory not installed, helper soft-fails."""
    real_import = __import__

    def _bad_import(name, *args, **kwargs):
        if name in ("aiforge_memory.api.http", "aiforge_memory.api.read"):
            raise ImportError("not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _bad_import)
    rows = uq._afm_bundle("any", repo="X", role="doer")
    assert rows == []


def test_query_skips_afm_when_no_repo(
    uq, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No repo arg + no env → afm_bundle never invoked."""
    monkeypatch.delenv("AIFORGE_AFM_REPO", raising=False)
    called = {"n": 0}

    def _spy(*a, **kw):
        called["n"] += 1
        return []

    monkeypatch.setattr(uq, "_afm_bundle", _spy)
    out = uq.query("hello world", limit=2)
    assert called["n"] == 0
    assert "afm_bundle" not in out["used_sources"]


def test_query_invokes_afm_when_repo_kwarg(
    uq, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit repo kwarg activates source #7."""
    captured = {}

    def _spy(text, *, repo, role):
        captured["repo"] = repo
        return [{"text": "[afm/repo_map]\nfoo", "score": 0.9}]

    monkeypatch.setattr(uq, "_afm_bundle", _spy)
    out = uq.query("hello", repo="MyRepo", limit=4)
    assert captured["repo"] == "MyRepo"
    assert "afm_bundle" in out["used_sources"]


def test_query_invokes_afm_via_env_fallback(
    uq, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No repo kwarg but AIFORGE_AFM_REPO set → activate."""
    monkeypatch.setenv("AIFORGE_AFM_REPO", "EnvRepo")
    captured = {}

    def _spy(text, *, repo, role):
        captured["repo"] = repo
        return [{"text": "[afm/conventions]\nx", "score": 0.9}]

    monkeypatch.setattr(uq, "_afm_bundle", _spy)
    out = uq.query("hello", limit=4)
    assert captured["repo"] == "EnvRepo"
    assert "afm_bundle" in out["used_sources"]


def test_query_disable_flag_blocks_afm(
    uq, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AIFORGE_AFM_BUNDLE_ENABLED=0 turns off source #7 even with repo."""
    monkeypatch.setenv("AIFORGE_AFM_BUNDLE_ENABLED", "0")
    called = {"n": 0}

    def _spy(*a, **kw):
        called["n"] += 1
        return []

    monkeypatch.setattr(uq, "_afm_bundle", _spy)
    uq.query("hello", repo="MyRepo", limit=2)
    assert called["n"] == 0


def test_query_afm_failure_isolated(
    uq, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backend exception in afm_bundle goes to errors, not raised."""
    def _boom(*a, **kw):
        raise RuntimeError("neo4j timeout")

    monkeypatch.setattr(uq, "_afm_bundle", _boom)
    out = uq.query("hello", repo="X", limit=2)
    assert any("afm_bundle: neo4j timeout" in e for e in out["errors"])
    assert "afm_bundle" not in out["used_sources"]


def test_default_weight_includes_afm_bundle(uq) -> None:
    assert "afm_bundle" in uq._DEFAULT_WEIGHTS
    assert uq._DEFAULT_WEIGHTS["afm_bundle"] > 0


# ── Gap #3: session/source diversification ──────────────────────────


def test_diversify_caps_per_group(uq) -> None:
    hits = [{"source": "memory", "ticket": "ONE-1", "text": str(i)}
            for i in range(5)]
    hits += [{"source": "doc", "text": "d1"},
             {"source": "doc", "text": "d2"}]
    out = uq._diversify(hits, per_group=3)
    # 3 kept from the ONE-1 flood + both doc rows = 5.
    assert len(out) == 5
    one1 = [h for h in out if h.get("ticket") == "ONE-1"]
    assert len(one1) == 3
    # highest-ranked survivors kept, order preserved.
    assert [h["text"] for h in one1] == ["0", "1", "2"]


def test_diversify_keys_by_source_without_ticket(uq) -> None:
    hits = [{"source": "doc", "text": str(i)} for i in range(4)]
    out = uq._diversify(hits, per_group=2)
    assert len(out) == 2
    assert [h["text"] for h in out] == ["0", "1"]


def test_diversify_passthrough_under_cap(uq) -> None:
    hits = [{"source": "memory", "text": "a"},
            {"source": "doc", "text": "b"}]
    out = uq._diversify(hits, per_group=3)
    assert out == hits


def test_diversify_disabled_when_per_group_zero(uq) -> None:
    hits = [{"source": "memory", "ticket": "ONE-1", "text": str(i)}
            for i in range(5)]
    out = uq._diversify(hits, per_group=0)
    assert out == hits


# ── Gap A7: cross-repo CALLS_REPO neighbour source ──────────────────


def test_default_weight_includes_xrepo(uq) -> None:
    assert "xrepo" in uq._DEFAULT_WEIGHTS
    assert uq._DEFAULT_WEIGHTS["xrepo"] > 0


def test_cross_repo_links_empty_on_import_failure(
    uq, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If aiforge_memory link store missing, helper soft-fails to []."""
    real_import = __import__

    def _bad_import(name, *args, **kwargs):
        if name.startswith("aiforge_memory.features.link") or \
                name.startswith("aiforge_memory.api.commands._driver"):
            raise ImportError("not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _bad_import)
    rows = uq._cross_repo_links("anything", repo="X")
    assert rows == []


def test_query_includes_xrepo_when_helper_returns_rows(
    uq, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Monkeypatched _cross_repo_links returns rows + repo set →
    'xrepo' appears in used_sources."""
    captured = {}

    def _spy(text, *, repo):
        captured["repo"] = repo
        return [{"text": "[xrepo] Y --rest--> X (conf 0.8)",
                 "score": 0.8, "source": "xrepo"}]

    monkeypatch.setattr(uq, "_cross_repo_links", _spy)
    out = uq.query("hello", repo="X", limit=4)
    assert captured["repo"] == "X"
    assert "xrepo" in out["used_sources"]


def test_query_skips_xrepo_when_disabled(
    uq, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AIFORGE_XREPO_ENABLED=0 turns off the source even with repo."""
    monkeypatch.setenv("AIFORGE_XREPO_ENABLED", "0")
    called = {"n": 0}

    def _spy(*a, **kw):
        called["n"] += 1
        return []

    monkeypatch.setattr(uq, "_cross_repo_links", _spy)
    out = uq.query("hello", repo="X", limit=2)
    assert called["n"] == 0
    assert "xrepo" not in out["used_sources"]


def test_query_skips_xrepo_when_no_repo(
    uq, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No repo known → cross-repo source never invoked."""
    monkeypatch.delenv("AIFORGE_AFM_REPO", raising=False)
    called = {"n": 0}

    def _spy(*a, **kw):
        called["n"] += 1
        return []

    monkeypatch.setattr(uq, "_cross_repo_links", _spy)
    out = uq.query("hello world", limit=2)
    assert called["n"] == 0
    assert "xrepo" not in out["used_sources"]
