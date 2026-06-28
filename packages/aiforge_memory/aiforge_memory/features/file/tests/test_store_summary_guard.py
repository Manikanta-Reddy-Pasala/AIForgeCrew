"""N1 — empty extract results must not wipe existing graph summaries.

A failed LLM pass yields FileSummary(summary="", skipped_reason=...).
The writer's Cypher must guard the SET so an empty summary / empty
purpose_tags list keeps whatever the graph already holds.
"""
from __future__ import annotations

from aiforge_memory.features.file import store as file_store
from aiforge_memory.features.file.extract import FileSummary


class _FakeResult:
    def consume(self):
        return None


class _FakeSession:
    def __init__(self, calls):
        self._calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def run(self, cypher, **params):
        self._calls.append((cypher, params))
        return _FakeResult()


class _FakeDriver:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def session(self):
        return _FakeSession(self.calls)


def test_update_cypher_guards_empty_summary_and_tags():
    assert "CASE WHEN $summary = ''" in file_store._UPDATE_FILE
    assert "coalesce(f.summary, '')" in file_store._UPDATE_FILE
    assert "size($purpose_tags) = 0" in file_store._UPDATE_FILE
    assert "coalesce(f.purpose_tags, [])" in file_store._UPDATE_FILE


def test_failed_summary_still_writes_skip_reason_but_counts_skipped():
    drv = _FakeDriver()
    counts = file_store.write_summaries(
        drv, repo="R",
        summaries=[FileSummary(repo="R", path="a.py",
                               skipped_reason="llm_error")],
    )
    assert counts == {"updated": 0, "skipped": 1}
    cypher, params = drv.calls[0]
    assert params["summary"] == ""
    assert params["skipped_reason"] == "llm_error"
    # The guard lives in the Cypher itself — the empty value goes over
    # the wire but the SET keeps the pre-existing graph summary.
    assert "CASE WHEN $summary = ''" in cypher


def test_good_summary_writes_normally():
    drv = _FakeDriver()
    counts = file_store.write_summaries(
        drv, repo="R",
        summaries=[FileSummary(repo="R", path="a.py",
                               summary="does X", purpose_tags=["x"])],
    )
    assert counts == {"updated": 1, "skipped": 0}
    _, params = drv.calls[0]
    assert params["summary"] == "does X"
    assert params["purpose_tags"] == ["x"]
