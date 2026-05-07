"""Tests for ``aiforge_core.runtime.parallel_doer``."""
from __future__ import annotations

import pytest

from aiforge_core.runtime import parallel_doer as pd


def _st(sid: str, *globs: str) -> pd.Subticket:
    return pd.Subticket(id=sid, scope_allowlist_globs=tuple(globs))


def test_empty_input_returns_empty():
    assert pd.batch([]) == []


def test_disjoint_scopes_batch_in_parallel():
    sts = [
        _st("a", "aiforge_core/runtime/**"),
        _st("b", "tests/python/**"),
        _st("c", "docs/**"),
    ]
    batches = pd.batch(sts, max_parallel=3)
    assert len(batches) == 1
    assert len(batches[0]) == 3


def test_overlapping_scopes_serialise():
    """A wildcard parent scope conflicts with any nested scope.

    a covers everything under aiforge_core/runtime/**, so b and c each
    conflict with a. b and c themselves are different files in the same
    dir → safe to pair. Expect: [a], [b, c]."""
    sts = [
        _st("a", "aiforge_core/runtime/**"),
        _st("b", "aiforge_core/runtime/foo.py"),
        _st("c", "aiforge_core/runtime/bar.py"),
    ]
    batches = pd.batch(sts, max_parallel=3)
    assert len(batches) == 2
    assert [st.id for st in batches[0]] == ["a"]
    assert sorted(st.id for st in batches[1]) == ["b", "c"]


def test_max_parallel_cap_enforced():
    """5 disjoint subtickets with max_parallel=2 → 3 batches (2,2,1)."""
    sts = [_st(f"s{i}", f"dir{i}/**") for i in range(5)]
    batches = pd.batch(sts, max_parallel=2)
    sizes = [len(b) for b in batches]
    assert sum(sizes) == 5
    assert max(sizes) == 2
    assert len(batches) == 3


def test_empty_globs_treated_as_conflicting():
    """Empty allowlist means 'no scope constraint' → conflict-by-default."""
    sts = [
        _st("a"),                # empty
        _st("b", "src/**"),
    ]
    batches = pd.batch(sts, max_parallel=3)
    assert len(batches) == 2


def test_max_parallel_below_one_rejected():
    with pytest.raises(ValueError):
        pd.batch([_st("a", "x/**")], max_parallel=0)


def test_nested_prefix_overlap_detected():
    sts = [
        _st("a", "aiforge_core/**"),
        _st("b", "aiforge_core/runtime/**"),
    ]
    batches = pd.batch(sts, max_parallel=3)
    assert len(batches) == 2  # nested prefix → conflict


def test_glob_with_wildcards_uses_literal_prefix():
    """Globs are reduced to their pre-wildcard literal prefix for compare."""
    sts = [
        _st("a", "src/foo/*.py"),
        _st("b", "src/bar/*.py"),     # disjoint after prefix
    ]
    batches = pd.batch(sts, max_parallel=3)
    assert len(batches) == 1
    assert len(batches[0]) == 2
