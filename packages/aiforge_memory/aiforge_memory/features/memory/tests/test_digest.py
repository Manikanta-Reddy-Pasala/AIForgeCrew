"""Gap M6 — cold-fact rollup / summarization (memory bound).

Unit-level: no live Neo4j. ``select_cold`` and ``build_digest`` are pure
and tested directly. ``run_digest`` is exercised with a recording fake
driver plus monkeypatched ``store.list_memory`` / ``store.upsert_observation``
so we assert the orchestration wiring without a database.
"""
from __future__ import annotations

import pytest

from aiforge_memory.features.memory import digest, store

_DAY = 86400.0


class _FakeResult:
    def __init__(self, rows=None):
        self._rows = rows or []

    def single(self):
        return self._rows[0] if self._rows else None

    def consume(self):
        return None

    def __iter__(self):
        return iter(self._rows)


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


def _row(text, *, age_days, tags=None, kind="note"):
    now = 1_000_000.0
    return {
        "id": text.replace(" ", "_"),
        "text": text,
        "kind": kind,
        "tags": list(tags or []),
        "created_at_epoch": now - age_days * _DAY,
    }


# --------------------------------------------------------------------------
# select_cold
# --------------------------------------------------------------------------

def test_select_cold_groups_by_first_tag():
    now = 1_000_000.0
    rows = [
        _row(f"fact {i}", age_days=120, tags=["payee", "x"])
        for i in range(5)
    ]
    groups = digest.select_cold(rows, now=now)
    assert "payee" in groups
    assert len(groups["payee"]) == 5


def test_select_cold_falls_back_to_kind_when_no_tags():
    now = 1_000_000.0
    rows = [_row(f"b {i}", age_days=200, tags=[], kind="bug") for i in range(6)]
    groups = digest.select_cold(rows, now=now)
    assert "bug" in groups
    assert len(groups["bug"]) == 6


def test_select_cold_drops_small_groups():
    now = 1_000_000.0
    rows = [_row(f"f {i}", age_days=120, tags=["topicA"]) for i in range(3)]
    groups = digest.select_cold(rows, now=now, min_group=5)
    assert groups == {}


def test_select_cold_drops_recent_rows():
    now = 1_000_000.0
    rows = [_row(f"f {i}", age_days=10, tags=["fresh"]) for i in range(8)]
    groups = digest.select_cold(rows, now=now, cold_days=90)
    assert groups == {}


def test_select_cold_mixed_only_old_count():
    now = 1_000_000.0
    rows = [_row(f"old {i}", age_days=120, tags=["t"]) for i in range(4)]
    rows += [_row(f"new {i}", age_days=5, tags=["t"]) for i in range(4)]
    # only 4 old -> below default min_group of 5 -> dropped
    assert digest.select_cold(rows, now=now) == {}
    # lower the threshold: only the 4 old ones survive
    groups = digest.select_cold(rows, now=now, min_group=4)
    assert len(groups["t"]) == 4


# --------------------------------------------------------------------------
# build_digest
# --------------------------------------------------------------------------

def test_build_digest_default_deterministic():
    rows = [_row(f"line {i}", age_days=120, tags=["t"]) for i in range(8)]
    d = digest.build_digest("t", rows)
    assert d["topic"] == "t"
    assert d["kind"] == "digest"
    assert d["count"] == 8
    assert len(d["source_ids"]) == 8
    assert "line 0" in d["text"]
    # deterministic fallback caps to ~5 bullets
    assert d["text"].count("\n") <= 5
    assert "line 7" not in d["text"]


def test_build_digest_uses_injected_summarizer():
    rows = [_row(f"line {i}", age_days=120, tags=["t"]) for i in range(8)]
    seen = {}

    def fake_sum(prompt: str) -> str:
        seen["prompt"] = prompt
        return "SUMMARY-OK"

    d = digest.build_digest("t", rows, summarize=fake_sum)
    assert d["text"] == "SUMMARY-OK"
    assert "line 0" in seen["prompt"]


# --------------------------------------------------------------------------
# run_digest
# --------------------------------------------------------------------------

def test_run_digest_builds_and_upserts(monkeypatch):
    now = 1_000_000.0
    rows = [_row(f"f {i}", age_days=120, tags=["payee"]) for i in range(6)]
    monkeypatch.setattr(store, "list_memory", lambda *a, **k: rows)

    upserts: list[dict] = []
    monkeypatch.setattr(
        store, "upsert_observation",
        lambda driver, **kw: upserts.append(kw) or {"id": "new"},
    )

    drv = _FakeDriver()
    out = digest.run_digest(drv, repo="R", now=now)

    assert out["groups"] == 1
    assert out["digests"] == 1
    assert len(upserts) == 1
    kw = upserts[0]
    assert kw["kind"] == "digest"
    assert "digest" in kw["tags"]
    assert "topic:payee" in kw["tags"]
    assert kw["dedupe"] is True
    # archive defaults off -> no cypher written
    assert out["archived"] == 0
    assert not drv.calls


def test_run_digest_archives_when_flag_set(monkeypatch):
    now = 1_000_000.0
    rows = [_row(f"f {i}", age_days=120, tags=["payee"]) for i in range(6)]
    monkeypatch.setattr(store, "list_memory", lambda *a, **k: rows)
    monkeypatch.setattr(
        store, "upsert_observation", lambda driver, **kw: {"id": "new"},
    )
    monkeypatch.setenv("AIFORGE_DIGEST_ARCHIVE", "1")

    drv = _FakeDriver()
    out = digest.run_digest(drv, repo="R", now=now, archive=True)

    assert out["archived"] == 6
    archive_calls = [c for c, _ in drv.calls if "archived" in c]
    assert archive_calls, "expected an archival cypher to run"


def test_run_digest_no_archive_when_flag_unset(monkeypatch):
    now = 1_000_000.0
    rows = [_row(f"f {i}", age_days=120, tags=["payee"]) for i in range(6)]
    monkeypatch.setattr(store, "list_memory", lambda *a, **k: rows)
    monkeypatch.setattr(
        store, "upsert_observation", lambda driver, **kw: {"id": "new"},
    )
    monkeypatch.delenv("AIFORGE_DIGEST_ARCHIVE", raising=False)

    drv = _FakeDriver()
    out = digest.run_digest(drv, repo="R", now=now, archive=True)

    assert out["archived"] == 0
    assert not any("archived" in c for c, _ in drv.calls)


def test_run_digest_nothing_cold(monkeypatch):
    now = 1_000_000.0
    rows = [_row(f"f {i}", age_days=5, tags=["payee"]) for i in range(6)]
    monkeypatch.setattr(store, "list_memory", lambda *a, **k: rows)
    called = {"n": 0}
    monkeypatch.setattr(
        store, "upsert_observation",
        lambda driver, **kw: called.__setitem__("n", called["n"] + 1),
    )

    drv = _FakeDriver()
    out = digest.run_digest(drv, repo="R", now=now)
    assert out == {"groups": 0, "digests": 0, "archived": 0}
    assert called["n"] == 0
