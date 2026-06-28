"""M7 — embed_config() is env-pluggable, defaults reproduce bge-m3/1024."""
from __future__ import annotations

from aiforge_memory.features.chunk import embed as em


def test_defaults_reproduce_bge_m3(monkeypatch) -> None:
    for k in ("AIFORGE_EMBED_MODEL", "AIFORGE_EMBED_URL", "AIFORGE_EMBED_DIM"):
        monkeypatch.delenv(k, raising=False)
    cfg = em.embed_config()
    assert cfg["model"] == "bge-m3"
    assert cfg["url"] == "http://127.0.0.1:8764"
    assert cfg["dim"] == 1024


def test_env_overrides_take_effect(monkeypatch) -> None:
    monkeypatch.setenv("AIFORGE_EMBED_MODEL", "nomic-embed-text")
    monkeypatch.setenv("AIFORGE_EMBED_URL", "http://10.0.0.5:9000/")
    monkeypatch.setenv("AIFORGE_EMBED_DIM", "768")
    cfg = em.embed_config()
    assert cfg["model"] == "nomic-embed-text"
    assert cfg["url"] == "http://10.0.0.5:9000/"
    assert cfg["dim"] == 768


def test_config_keys_present(monkeypatch) -> None:
    cfg = em.embed_config()
    assert set(cfg) == {"model", "url", "dim"}


def test_default_embed_url_constant_unchanged() -> None:
    # backward-compat: legacy callers reading the module constant still work
    assert em.embed_config()["url"] == em.DEFAULT_EMBED_URL
