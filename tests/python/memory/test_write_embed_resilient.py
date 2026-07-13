"""Write-path must never lose a note when the embedder can't load a model.

The recall path stays loud (semantic errors surface) but a *write* degrades to
store-without-vector: the note is persisted (findable via keyword/FTS) and the
missing vector is backfilled later by reembed_all once the model is available.
"""
from __future__ import annotations
import importlib
import pytest


@pytest.fixture()
def mem(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "m.db"))
    # hash backend keeps _init_vec off (no sqlite_vec in dev); the write-path
    # resilience under test is backend-agnostic — we break embed() directly.
    monkeypatch.setenv("AIFORGE_EMBED_BACKEND", "hash")
    import aiforge_core.memory.sqlite_memory as sm
    importlib.reload(sm)
    return sm


def _break_embed(monkeypatch):
    """Make the active embedder raise like a missing/unloadable model."""
    from aiforge_core.memory import local_embed
    def boom(_text):
        raise RuntimeError(
            "semantic embedder could not load model "
            "'sentence-transformers/all-MiniLM-L6-v2'")
    monkeypatch.setattr(local_embed, "embed", boom)


def test_write_survives_embed_failure(mem, monkeypatch):
    _break_embed(monkeypatch)
    rid = mem.write_unit(
        text="H2-2 system is on subnet 10.130.112.x",
        kind="note", tags=["networking", "h2"])
    assert rid > 0                                   # note persisted, not lost
    with mem._conn() as c:
        row = c.execute(
            "SELECT text, embedding FROM memory_units WHERE id = ?",
            (rid,)).fetchone()
    assert "10.130.112" in row["text"]
    assert row["embedding"] == "[]"                  # deferred, sentinel


def test_deferred_write_is_backfilled_on_reembed(mem, monkeypatch):
    _break_embed(monkeypatch)
    rid = mem.write_unit(text="usbguard locks USB ports", kind="note")
    assert rid > 0
    # model comes back: reembed backfills the deferred row's vector
    from aiforge_core.memory import local_embed
    monkeypatch.setattr(local_embed, "embed", lambda t: [0.1, 0.2, 0.3])
    mem.reembed_all()
    with mem._conn() as c:
        row = c.execute(
            "SELECT embedding FROM memory_units WHERE id = ?", (rid,)).fetchone()
    assert row["embedding"] != "[]"                  # vector now present


def test_reembed_skips_rows_the_model_still_cant_embed(mem, monkeypatch):
    """A half-broken model must not abort the whole backfill batch."""
    from aiforge_core.memory import local_embed
    monkeypatch.setattr(local_embed, "embed", lambda t: [0.1, 0.2, 0.3])
    good = mem.write_unit(text="deploy is git pull then restart", kind="note")
    # now the model breaks again before a maintenance reembed
    def boom(_t):
        raise RuntimeError("model gone")
    monkeypatch.setattr(local_embed, "embed", boom)
    res = mem.reembed_all()                           # must NOT raise
    assert res["reembedded"] == 0
    assert res.get("failed", 0) >= 1
    # the previously-good vector is left intact, not clobbered
    with mem._conn() as c:
        row = c.execute(
            "SELECT embedding FROM memory_units WHERE id = ?", (good,)).fetchone()
    assert row["embedding"] != "[]"
