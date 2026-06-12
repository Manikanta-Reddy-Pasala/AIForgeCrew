"""Dynamic local-model resolution in ``aiforge_core.config.agent_config``.

The hardcoded Mac-Studio model path is gone; the local default now
resolves env pin → /v1/models discovery → legacy fallback constant.
"""
from __future__ import annotations

import pytest

from aiforge_core.config import agent_config as ac


@pytest.fixture(autouse=True)
def _clean_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("AIFORGE_LOCAL_DEFAULT_MODEL", raising=False)
    ac._LOCAL_DEFAULT_CACHE[0] = 0.0
    ac._LOCAL_DEFAULT_CACHE[1] = None
    yield
    ac._LOCAL_DEFAULT_CACHE[0] = 0.0
    ac._LOCAL_DEFAULT_CACHE[1] = None


def test_env_pin_wins(monkeypatch):
    monkeypatch.setenv("AIFORGE_LOCAL_DEFAULT_MODEL", "my/pinned-model")
    assert ac._local_default_model() == "my/pinned-model"


def test_discovery_first_id_used(monkeypatch):
    monkeypatch.setattr(
        ac, "_discover_local_models",
        lambda: [{"id": "served/model-a"}, {"id": "served/model-b"}],
    )
    assert ac._local_default_model() == "served/model-a"
    # cached on second call even if discovery would now fail
    monkeypatch.setattr(ac, "_discover_local_models",
                        lambda: (_ for _ in ()).throw(RuntimeError))
    assert ac._local_default_model() == "served/model-a"


def test_fallback_when_server_dead(monkeypatch):
    monkeypatch.setattr(ac, "_discover_local_models", lambda: [])
    assert ac._local_default_model() == ac._LOCAL_FALLBACK_MODEL


def test_defaults_use_dynamic_model(monkeypatch):
    monkeypatch.setenv("AIFORGE_LOCAL_DEFAULT_MODEL", "dyn/model")
    cfg = ac.load_all()
    assert cfg["doer"]["model"] == "dyn/model"
    assert cfg["planner"]["provider"] == "local"


def test_role_env_override_still_wins(monkeypatch):
    monkeypatch.setenv("AIFORGE_LOCAL_DEFAULT_MODEL", "dyn/model")
    monkeypatch.setenv("AIFORGE_DOER_MODEL", "role/override")
    cfg = ac.load_all()
    assert cfg["doer"]["model"] == "role/override"
    assert cfg["verifier"]["model"] == "dyn/model"


def test_local_catalog_is_discovery_only():
    assert ac.MODEL_CATALOG["local"] == []


def test_list_providers_local_default_resolves(monkeypatch):
    monkeypatch.setenv("AIFORGE_LOCAL_DEFAULT_MODEL", "dyn/model")
    row = next(p for p in ac.list_providers() if p["id"] == "local")
    assert row["default_model"] == "dyn/model"


def test_apply_profile_local_resolves_dynamic(monkeypatch):
    monkeypatch.setenv("AIFORGE_LOCAL_DEFAULT_MODEL", "dyn/model")
    out = ac.apply_profile("local")
    assert all(row["model"] == "dyn/model" for row in out.values())


def test_model_router_removed():
    with pytest.raises(ImportError):
        import aiforge_core.runtime.model_router  # noqa: F401
