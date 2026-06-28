"""N6 — bundle helpers must surface backend failures via the errors
list instead of silently returning []."""
from __future__ import annotations

from unittest.mock import MagicMock

from aiforge_memory.query import bundle


def _broken_driver():
    drv = MagicMock()
    drv.session.side_effect = RuntimeError("bolt down")
    return drv


def test_each_helper_appends_its_source_tag():
    cases = [
        ("chunks", lambda d, e: bundle._chunks_for(
            d, repo="r", paths=["a.py"], errors=e)),
        ("decisions", lambda d, e: bundle._decisions_for(
            d, repo="r", paths=["a.py"], fqnames=[], errors=e)),
        ("observations", lambda d, e: bundle._observations_for(
            d, repo="r", paths=["a.py"], fqnames=[], errors=e)),
        ("notes", lambda d, e: bundle._notes_for(
            d, repo="r", paths=["a.py"], fqnames=[], errors=e)),
        ("docs", lambda d, e: bundle._docs_for(
            d, repo="r", paths=["a.py"], fqnames=[], errors=e)),
        ("cross_repo", lambda d, e: bundle._cross_repo_for(
            d, repo="r", errors=e)),
        ("repo_map", lambda d, e: bundle._repo_map_for(
            d, repo="r", focal_paths=["a.py"], errors=e)),
        ("vector_observations", lambda d, e: bundle._vector_observations(
            d, repo="r", query_vec=[0.1] * 4, errors=e)),
    ]
    for source, fn in cases:
        errors: list[str] = []
        out = fn(_broken_driver(), errors)
        assert out in ([], ""), source
        assert any(err.startswith(f"{source}") and "bolt down" in err
                   for err in errors), (source, errors)


def test_helpers_stay_silent_without_errors_list():
    """errors=None (default) keeps the old swallow-and-empty contract."""
    assert bundle._chunks_for(_broken_driver(), repo="r",
                              paths=["a.py"]) == []
    assert bundle._cross_repo_for(_broken_driver(), repo="r") == []


def test_vector_observations_tags_ppr_fallback_failure():
    errors: list[str] = []
    bundle._vector_observations(_broken_driver(), repo="r",
                                query_vec=[0.1] * 4, errors=errors)
    # Both the PPR attempt and the vanilla fallback hit the broken
    # driver — each path is tagged.
    assert any(e.startswith("vector_observations(ppr):") for e in errors)
    assert any(e.startswith("vector_observations:") for e in errors)
