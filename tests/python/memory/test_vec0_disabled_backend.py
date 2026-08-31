"""A store built under a semantic backend must stay WRITABLE when the process
later comes up with the vector backend off.

The vec0 sync triggers are persistent schema. Nothing on the backend-off path
loads sqlite-vec, so every leftover trigger turned an ordinary memory write
into ``no such module: vec0`` — and the ``_disable_vec`` rescue never ran,
because it hangs off ``_init_vec``, which that path skips. Losing a note that
way is the exact failure ``_safe_embed`` already rules out for the embedder.
"""
from __future__ import annotations

import struct

import pytest

sqlite_vec = pytest.importorskip(
    "sqlite_vec", reason="the vec0 module under test ships with sqlite-vec")


_DIM = 32          # pinned: see the fixture


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A scratch store whose embedder reports ONE fixed dimension.

    Pinning it is not cosmetic. `embed_dim()` answers for whichever backend is
    selected at the moment it is called, and these tests deliberately switch
    backends mid-test — so a vector written while the lexical backend was
    active could be a different width than the vec0 table, its backfill insert
    would be skipped, and the test would fail for that reason instead of the
    one it is checking. (It did: `(2, 1) != (2, 2)`, and only when run after
    the rest of tests/python/memory, which is exactly the kind of
    order-dependent result that teaches the wrong lesson.) The skip-on-mismatch
    behaviour is real and is covered on its own below.
    """
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    from aiforge_core.memory import local_embed
    from aiforge_core.memory.sqlite_memory import _schema
    monkeypatch.setattr(local_embed, "embed_dim", lambda: _DIM)
    monkeypatch.setattr(_schema, "_VEC_WARNED", False, raising=False)
    return _schema


def _vec(x: float, dim: int = _DIM) -> bytes:
    return struct.pack(f"{dim}f", *([x] * dim))


def _write(schema, text: str, x: float, dim: int = _DIM) -> None:
    with schema._conn() as c:
        c.execute("INSERT INTO memory_units(kind, text, embedding) VALUES (?,?,?)",
                  ("note", text, _vec(x, dim)))


def _counts(schema) -> tuple[int, int, list[str]]:
    with schema._conn() as c:
        units = c.execute("SELECT count(*) FROM memory_units").fetchone()[0]
        try:
            vecs = c.execute("SELECT count(*) FROM vec_memory").fetchone()[0]
        except Exception:                      # noqa: BLE001 — index absent
            vecs = -1
        trg = [r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND name LIKE 'vec_memory_%'")]
    return units, vecs, trg


def test_write_survives_backend_switched_off(store, monkeypatch):
    """The reported bug: built under model2vec, reopened with the backend unset
    (default 'hash') → every write died on ``no such module: vec0``."""
    monkeypatch.setenv("AIFORGE_EMBED_BACKEND", "model2vec")
    _write(store, "written while semantic", 0.1)
    assert _counts(store)[2], "precondition: the semantic run installs the triggers"

    monkeypatch.delenv("AIFORGE_EMBED_BACKEND", raising=False)   # default = hash
    assert not store._vec_enabled()
    _write(store, "written while off", 0.2)          # must not raise

    units, _, triggers = _counts(store)
    assert units == 2, "the note written with the backend off must be stored"
    assert triggers == [], "the stale vec0 triggers are retired, not left to fail"


def test_index_gap_is_backfilled_when_the_backend_returns(store, monkeypatch):
    """Retiring the triggers is only acceptable because the gap closes: a row
    written while the backend was off rejoins the index on the way back."""
    monkeypatch.setenv("AIFORGE_EMBED_BACKEND", "model2vec")
    _write(store, "a", 0.1)
    monkeypatch.delenv("AIFORGE_EMBED_BACKEND", raising=False)
    _write(store, "b (while off)", 0.2)

    monkeypatch.setenv("AIFORGE_EMBED_BACKEND", "model2vec")
    units, vecs, triggers = _counts(store)
    assert (units, vecs) == (2, 2), "the row written while off is backfilled"
    assert len(triggers) == 3, "the sync triggers come back with the backend"

    _write(store, "c (after restore)", 0.3)
    assert _counts(store)[:2] == (3, 3), "normal indexing resumes"


def test_retire_is_a_noop_on_a_store_that_never_had_vectors(store, monkeypatch):
    """A lexical-only store must not pay for this — no triggers, no log, no
    error, and the write still lands."""
    monkeypatch.setenv("AIFORGE_EMBED_BACKEND", "hash")
    _write(store, "lexical only", 0.1)
    units, vecs, triggers = _counts(store)
    assert units == 1
    assert triggers == []
    assert vecs == -1, "no vec index was ever created"


def test_dim_mismatched_row_is_skipped_not_fatal(store, monkeypatch):
    """The honest limit of the recovery above.

    A row written under a lexical backend carries THAT backend's vector. If it
    is a different width than the active vec0 table, the backfill cannot insert
    it — and must skip it rather than abort, leaving the note stored and
    keyword-findable until `aiforge-maint memory reembed` re-embeds it.
    """
    monkeypatch.setenv("AIFORGE_EMBED_BACKEND", "model2vec")
    _write(store, "right width", 0.1)
    monkeypatch.delenv("AIFORGE_EMBED_BACKEND", raising=False)
    _write(store, "wrong width, written while off", 0.2, dim=_DIM // 2)

    monkeypatch.setenv("AIFORGE_EMBED_BACKEND", "model2vec")
    units, vecs, triggers = _counts(store)          # must not raise
    assert units == 2, "the note is stored regardless of what the index can take"
    assert vecs == 1, "the mismatched row is skipped, not force-inserted"
    assert len(triggers) == 3, "one unbackfillable row does not disable the index"
