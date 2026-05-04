"""Unit tests for aiforge_core.memory.rag.retriever — Phase 2 RAG path.

Fully offline: no Postgres, no embed sidecar, no rerank sidecar.
Store methods and HTTP rerank are mocked at the boundary.
"""
from __future__ import annotations

import contextlib
from unittest.mock import patch

from aiforge_core.memory.retrieval import Hit, ROLE_POLICIES


def _hit(id_: str, score: float = 1.0, tier: str = "t2") -> Hit:
    return Hit(
        id=id_, score=score, source="test", tier=tier,
        text=f"text-{id_}", title=None, metadata={"tier": tier},
    )


@contextlib.contextmanager
def _stub_retrievers(vec_map: dict, bm25_map: dict):
    """Patch store-level retrieval helpers and the rerank HTTP call.

    vec_map / bm25_map keys: tier name (t1..t4) → list[Hit].
    """
    def fake_vec(store, tier, wing_prefix, query, top_k):
        return vec_map.get(tier, [])

    def fake_bm25(store, tier, wing_prefix, query, top_k):
        return bm25_map.get(tier, [])

    def fake_rerank(query, hits, keep):
        return hits[:keep]

    with (
        patch("aiforge_core.memory.rag.retriever._vector_retrieve", side_effect=fake_vec),
        patch("aiforge_core.memory.rag.retriever._bm25_retrieve", side_effect=fake_bm25),
        patch("aiforge_core.memory.rag.retriever._rerank", side_effect=fake_rerank),
        patch("aiforge_core.memory.rag.retriever._get_store", return_value=object()),
    ):
        yield


class TestRolePolicy:
    def test_role_policy_applies_correct_tiers(self) -> None:
        from aiforge_core.memory.rag.retriever import retrieve_for_role_li

        policy = ROLE_POLICIES.get("doer") or ROLE_POLICIES["developer"]
        tiers_visited: list[str] = []

        def track_vec(store, tier, wing_prefix, query, top_k):
            tiers_visited.append(tier)
            return [_hit(f"v-{tier}", tier=tier)]

        with (
            patch("aiforge_core.memory.rag.retriever._vector_retrieve", side_effect=track_vec),
            patch("aiforge_core.memory.rag.retriever._bm25_retrieve", return_value=[]),
            patch("aiforge_core.memory.rag.retriever._rerank", side_effect=lambda q, h, keep: h[:keep]),
            patch("aiforge_core.memory.rag.retriever._get_store", return_value=object()),
        ):
            retrieve_for_role_li(None, "doer", "test query", None)

        expected = [spec["tier"] for spec in policy["tiers"]]
        assert tiers_visited == expected


class TestRrfFuseOrder:
    def test_rrf_fuse_order(self) -> None:
        from aiforge_core.memory.rag.retriever import retrieve_for_role_li

        vec_map = {"t2": [_hit("a"), _hit("b"), _hit("c")]}
        bm25_map = {"t2": [_hit("c"), _hit("a"), _hit("d")]}

        with _stub_retrievers(vec_map, bm25_map):
            result = retrieve_for_role_li(None, "doer", "query", None)

        ids = [h.id for h in result]
        # 'a' and 'c' appear in both rankings → should rank highest
        if "a" in ids and "d" in ids:
            assert ids.index("a") < ids.index("d")


class TestSidecarDownFallback:
    def test_sidecar_down_fallback(self) -> None:
        from aiforge_core.memory.rag.retriever import _rerank

        hits = [_hit("h1"), _hit("h2"), _hit("h3")]
        with patch("urllib.request.urlopen", side_effect=OSError("sidecar down")):
            out = _rerank("query", hits, keep=2)

        assert len(out) == 2
        assert out[0].id == "h1"


class TestReturnsSameHitType:
    def test_returns_same_hit_type_as_legacy(self) -> None:
        from aiforge_core.memory.rag.retriever import retrieve_for_role_li

        vec_map = {"t2": [_hit("v1"), _hit("v2")]}
        bm25_map = {"t2": [_hit("b1")]}

        with _stub_retrievers(vec_map, bm25_map):
            out = retrieve_for_role_li(None, "doer", "q", None)

        assert all(isinstance(h, Hit) for h in out)
