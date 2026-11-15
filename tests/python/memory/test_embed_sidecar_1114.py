"""AIFORGE_EMBED_BACKEND=sidecar: memory vectors from the local bge-m3 service."""
from __future__ import annotations

from aiforge_core.memory import embed as side
from aiforge_core.memory import local_embed as L


def test_sidecar_backend_embeds_through_the_service(monkeypatch):
    monkeypatch.setenv("AIFORGE_EMBED_BACKEND", "sidecar")
    L._SIDE_DIM.clear()
    seen = []
    monkeypatch.setattr(side, "embed", lambda t: seen.append(t) or [0.1] * 1024)
    assert len(L.embed("mask a phone number")) == 1024
    assert L.embed_dim() == 1024
    assert L.embed_signature().startswith("sidecar:")
    assert L.embed("   ") and seen[-1] == "."          # empty text still embeds


def test_other_backends_are_unchanged(monkeypatch):
    monkeypatch.setenv("AIFORGE_EMBED_BACKEND", "hash")
    assert L.embed_signature() == "hash"
    assert len(L.embed("x")) == L.EMBED_DIM


def test_the_sidecar_backend_uses_the_vector_index(monkeypatch):
    """Live: AIFORGE_EMBED_BACKEND=sidecar left the vec0 index at the old hash
    dimension and recall on a brute-force scan — _vec_enabled did not list it."""
    from aiforge_core.memory.sqlite_memory import _schema
    for b in ("sidecar", "bge", "bge-m3"):
        monkeypatch.setenv("AIFORGE_EMBED_BACKEND", b)
        assert _schema._vec_enabled()
    monkeypatch.setenv("AIFORGE_EMBED_BACKEND", "hash")
    assert not _schema._vec_enabled()
