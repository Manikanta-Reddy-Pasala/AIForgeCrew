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


def test_stale_bare_local_row_does_not_shadow_cloud_global(tmp_path, monkeypatch):
    """The reported bug: operator sets a cloud/internal global default, but a
    leftover bare-local per-role row keeps that role on 127.0.0.1:1234."""
    import json
    import importlib
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    for k in ("AIFORGE_TRIAGE_BASE_URL", "AIFORGE_TRIAGE_PROVIDER",
              "AIFORGE_TRIAGE_MODEL", "AIFORGE_LM_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    (tmp_path / "agent_config.json").write_text(json.dumps({
        "_default": {"provider": "openai_compatible", "model": "qwen35-122b",
                     "base_url": "https://chat.ai.internal/v1"},
        "triage": {"provider": "local", "model": "/old/mlx/path",
                   "base_url": None, "api_key": None, "insecure_tls": False},
    }))
    import aiforge_core.config.agent_config as ac
    importlib.reload(ac)
    r = ac.resolve_litellm("triage")
    assert "chat.ai.internal" in r["api_base"]
    assert "qwen35-122b" in r["model_id"]


def test_explicit_per_role_local_url_still_wins_over_global(tmp_path, monkeypatch):
    import json
    import importlib
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("AIFORGE_TRIAGE_BASE_URL", raising=False)
    (tmp_path / "agent_config.json").write_text(json.dumps({
        "_default": {"provider": "openai_compatible", "model": "x",
                     "base_url": "https://chat.ai.internal/v1"},
        "triage": {"provider": "local", "model": "m",
                   "base_url": "http://127.0.0.1:1235/v1"},
    }))
    import aiforge_core.config.agent_config as ac
    importlib.reload(ac)
    assert "1235" in ac.resolve_litellm("triage")["api_base"]


def test_reset_wipes_config(tmp_path, monkeypatch):
    import json
    import importlib
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    import aiforge_core.config.agent_config as ac
    importlib.reload(ac)
    (tmp_path / "agent_config.json").write_text(json.dumps({
        "_default": {"provider": "openai_compatible", "model": "m", "base_url": "https://x/v1"},
        "triage": {"provider": "local", "model": "stale", "base_url": None},
    }))
    # keep_default → only per-role rows removed
    ac.reset(keep_default=True)
    left = json.loads((tmp_path / "agent_config.json").read_text())
    assert list(left.keys()) == ["_default"]
    # full reset → file gone, then idempotent
    assert ac.reset()["removed"] is True
    assert not (tmp_path / "agent_config.json").exists()
    assert ac.reset()["removed"] is False


def test_reset_clears_caches(tmp_path, monkeypatch):
    import importlib
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    import aiforge_core.config.agent_config as ac
    importlib.reload(ac)
    ac._LOCAL_DEFAULT_CACHE[0] = 9e9
    ac._LOCAL_DEFAULT_CACHE[1] = "stale-model"
    ac._CATALOG_CACHE["local"] = (9e9, [{"id": "stale"}])
    ac.reset()
    assert ac._LOCAL_DEFAULT_CACHE[1] is None
    assert not ac._CATALOG_CACHE


def test_explicit_insecure_tls_false_not_overridden(tmp_path, monkeypatch):
    import json
    import importlib
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("AIFORGE_PLANNER_BASE_URL", raising=False)
    (tmp_path / "agent_config.json").write_text(json.dumps({
        "_default": {"provider": "openai_compatible", "model": "m",
                     "base_url": "https://x/v1", "insecure_tls": True},
        "planner": {"provider": "openai_compatible", "model": "m",
                    "base_url": "https://x/v1", "insecure_tls": False},
    }))
    import aiforge_core.config.agent_config as ac
    importlib.reload(ac)
    assert ac.get("planner")["insecure_tls"] is False   # explicit false preserved
