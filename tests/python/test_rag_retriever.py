"""Unit tests for aiforge_core.rag.retriever — Phase 2 LlamaIndex RAG.

All tests are fully offline: no Postgres, no embed sidecar, no rerank sidecar.
LlamaIndex retrievers and HTTP calls are mocked at the boundary.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from aiforge_core.retrieval import Hit, ROLE_POLICIES


# ─────────────────── helpers ────────────────────────────────────────────────

def _make_node(node_id: str, text: str, tier: str = "t2", score: float = 1.0):
    """Build a fake LlamaIndex NodeWithScore-like object."""
    node = SimpleNamespace(
        node_id=node_id,
        metadata={"tier": tier, "source": "test", "title": None},
        get_content=lambda: text,
    )
    return SimpleNamespace(node=node, score=score)


def _patch_retrievers(vec_nodes, bm25_nodes):
    """Context manager that stubs both VectorIndexRetriever and BM25Retriever."""
    import contextlib

    @contextlib.contextmanager
    def _ctx():
        mock_vec = MagicMock()
        mock_vec.retrieve.return_value = vec_nodes

        mock_bm25 = MagicMock()
        mock_bm25.retrieve.return_value = bm25_nodes

        with (
            patch(
                "aiforge_core.rag.retriever.VectorIndexRetriever",
                return_value=mock_vec,
            ),
            patch(
                "aiforge_core.rag.retriever._bm25_retrieve",
                return_value=[
                    Hit(
                        id=n.node.node_id,
                        score=n.score,
                        source=n.node.metadata.get("source"),
                        tier=n.node.metadata.get("tier"),
                        text=n.node.get_content(),
                        title=n.node.metadata.get("title"),
                        metadata=n.node.metadata,
                    )
                    for n in bm25_nodes
                ],
            ),
        ):
            yield

    return _ctx()


# ─────────────────── 1. role_policy_applies_correct_tiers ────────────────────

class TestRolePolicy:
    def test_role_policy_applies_correct_tiers(self) -> None:
        """AC: retrieve_for_role_li calls vector retrieve once per tier in policy."""
        from llama_index.core import VectorStoreIndex

        mock_index = MagicMock(spec=VectorStoreIndex)
        role = "planner"
        policy = ROLE_POLICIES[role]
        tier_count = len(policy["tiers"])

        vec_call_args: list[tuple] = []

        def fake_vec_retrieve(index, query, top_k):
            vec_call_args.append((query, top_k))
            return []

        def fake_bm25_retrieve(index, query, top_k):
            return []

        with (
            patch("aiforge_core.rag.retriever._vector_retrieve", side_effect=fake_vec_retrieve),
            patch("aiforge_core.rag.retriever._bm25_retrieve", side_effect=fake_bm25_retrieve),
            patch("aiforge_core.rag.retriever._rerank", return_value=[]),
        ):
            from aiforge_core.rag.retriever import retrieve_for_role_li

            retrieve_for_role_li(mock_index, role="planner", query="find auth code", parent_id=None)

        assert len(vec_call_args) == tier_count, (
            f"expected {tier_count} vector calls (one per tier), got {len(vec_call_args)}"
        )
        queried_top_ks = [c[1] for c in vec_call_args]
        expected_top_ks = [s["top_k"] for s in policy["tiers"]]
        assert queried_top_ks == expected_top_ks


# ─────────────────── 2. rrf_fuse_order ──────────────────────────────────────

class TestRrfFuseOrder:
    def test_rrf_fuse_order(self) -> None:
        """AC: higher-ranked hits across both retrievers appear first after RRF."""
        from llama_index.core import VectorStoreIndex

        mock_index = MagicMock(spec=VectorStoreIndex)

        top_vec = _make_node("vec-top", "vector top result", score=0.99)
        low_vec = _make_node("vec-low", "vector low result", score=0.5)
        top_bm25 = _make_node("bm-top", "bm25 top result", score=0.9)

        captured_fused: list[list[Hit]] = []

        original_rrf = __import__(
            "aiforge_core.retrieval", fromlist=["rrf_fuse"]
        ).rrf_fuse

        def spy_rrf(rankings, **kwargs):
            result = original_rrf(rankings, **kwargs)
            captured_fused.append(result)
            return result

        with (
            patch(
                "aiforge_core.rag.retriever._vector_retrieve",
                return_value=[
                    Hit(id="vec-top", score=0.99, text="vector top result", tier="t2", metadata={}),
                    Hit(id="vec-low", score=0.5, text="vector low result", tier="t2", metadata={}),
                ],
            ),
            patch(
                "aiforge_core.rag.retriever._bm25_retrieve",
                return_value=[
                    Hit(id="bm-top", score=0.9, text="bm25 top result", tier="t2", metadata={}),
                ],
            ),
            patch("aiforge_core.rag.retriever.rrf_fuse", side_effect=spy_rrf),
            patch("aiforge_core.rag.retriever._rerank", side_effect=lambda q, hits, keep: hits[:keep]),
        ):
            from aiforge_core.rag.retriever import retrieve_for_role_li

            result = retrieve_for_role_li(mock_index, role="supervisor", query="test", parent_id=None)

        assert len(captured_fused) >= 1
        fused = captured_fused[0]
        ids = [h.id for h in fused]
        assert "vec-top" in ids
        assert "bm-top" in ids
        # vec-top ranked 1st in vec list → should beat vec-low in RRF
        vec_top_pos = ids.index("vec-top")
        vec_low_pos = ids.index("vec-low")
        assert vec_top_pos < vec_low_pos


# ─────────────────── 3. sidecar_down_fallback ───────────────────────────────

class TestSidecarDownFallback:
    def test_sidecar_down_fallback(self) -> None:
        """AC: when rerank HTTP raises, result is RRF-ordered hits (not empty)."""
        import urllib.error

        from llama_index.core import VectorStoreIndex

        mock_index = MagicMock(spec=VectorStoreIndex)
        hits = [
            Hit(id="h1", score=0.9, text="first", tier="t1", metadata={}),
            Hit(id="h2", score=0.7, text="second", tier="t1", metadata={}),
        ]

        with (
            patch(
                "aiforge_core.rag.retriever._vector_retrieve",
                return_value=hits,
            ),
            patch("aiforge_core.rag.retriever._bm25_retrieve", return_value=[]),
            patch(
                "urllib.request.urlopen",
                side_effect=OSError("Connection refused"),
            ),
        ):
            from aiforge_core.rag.retriever import _rerank

            result = _rerank("test query", hits, keep=5)

        assert len(result) > 0
        assert result[0].id == "h1"


# ─────────────────── 4. returns_same_hit_type_as_legacy ─────────────────────

class TestReturnsSameHitType:
    def test_returns_same_hit_type_as_legacy(self) -> None:
        """AC: retrieve_for_role_li returns list[Hit] identical in shape to legacy."""
        from llama_index.core import VectorStoreIndex

        mock_index = MagicMock(spec=VectorStoreIndex)
        fake_hit = Hit(
            id="mem:42",
            score=0.88,
            source="test",
            tier="t3",
            text="some skill text",
            title="skill title",
            metadata={"wing": "skills/python"},
        )

        with (
            patch(
                "aiforge_core.rag.retriever._vector_retrieve",
                return_value=[fake_hit],
            ),
            patch("aiforge_core.rag.retriever._bm25_retrieve", return_value=[]),
            patch(
                "aiforge_core.rag.retriever._rerank",
                side_effect=lambda q, hits, keep: hits[:keep],
            ),
        ):
            from aiforge_core.rag.retriever import retrieve_for_role_li

            result = retrieve_for_role_li(
                mock_index, role="doer", query="implement search", parent_id=None
            )

        assert isinstance(result, list)
        for item in result:
            assert isinstance(item, Hit), f"expected Hit, got {type(item)}"
            assert hasattr(item, "id")
            assert hasattr(item, "score")
            assert hasattr(item, "text")
            assert hasattr(item, "tier")
            assert hasattr(item, "source")
            assert hasattr(item, "metadata")
            assert hasattr(item, "title")
