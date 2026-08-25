"""Diff-aware test impact map (standards gap C12).

Given a set of changed file paths, return the test files likely impacted.
This was backed by an optional code-graph (File_v2 / Symbol_v2) that has
been removed — this is a SQLite-only build — so it now soft-fails to an
empty list and the caller runs the full test suite.

This pairs with the ``run_tests`` tool: pass ``pattern`` =
comma-joined output of ``impacted_tests(changed_paths)`` so ``pytest
-k`` / ``mvn -Dtest=`` only runs the relevant slice.
"""
from __future__ import annotations

import logging

log = logging.getLogger("aiforge.diff_impact")


def impacted_tests(
    _repo: str,
    _changed_paths: list[str],
    *,
    hops: int = 3,
    driver=None,
) -> list[str]:
    """Return test-file paths likely impacted by ``changed_paths``.

    The code-graph backend was removed, so this always returns [] — callers
    treat empty as "fall back to full suite".
    """
    return []
