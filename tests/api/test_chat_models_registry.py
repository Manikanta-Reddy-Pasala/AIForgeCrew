"""`/api/chat/models` lists CONFIGURED models (registry), not just loaded ones.

A local model host (LM Studio) exposes only *loaded* models over HTTP, so a
loaded-only picker hides every model the user added but hasn't loaded. The chat
picker unions the machine-agnostic model registry with the currently-served set,
marks each ``active`` = loaded, excludes embeddings, and never loads anything.
"""
from __future__ import annotations

import importlib


def _api(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_DB_PATH", str(tmp_path / "api.db"))
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    for k in ("AIFORGE_MEMORY_BACKEND", "AIFORGE_NEO4J_URI", "NEO4J_URI",
              "AIFORGE_PG_URL", "AIFORGE_FORCE_PG"):
        monkeypatch.delenv(k, raising=False)
    import aiforge_core.api.api as api
    importlib.reload(api)
    return api


def test_chat_models_unions_registry_with_served(monkeypatch, tmp_path):
    api = _api(monkeypatch, tmp_path)

    # Registry has 3 configured models incl. an embedding; only one is loaded.
    monkeypatch.setattr(api, "_served_model_ids_for_role",
                        lambda role: {"qwen/qwen3-coder-next"})
    monkeypatch.setattr(api._acfg, "get",
                        lambda role: {"provider": "local",
                                      "model": "qwen/qwen3-coder-next"})
    monkeypatch.setattr(api._acfg, "archetypes", lambda: ["chat"])

    import aiforge_core.config.model_registry as mr
    monkeypatch.setattr(mr, "list_models", lambda: [
        {"model": "qwen/qwen3-coder-next", "label": "qwen"},
        {"model": "gemma/gemma-4-26b", "label": "gemma"},
        {"model": "text-embedding-nomic", "label": "embed"},
    ])

    out = api.chat_models()
    ids = [m["id"] for m in out["models"]]

    # embedding excluded; both LLMs present even though gemma isn't loaded
    assert "text-embedding-nomic" not in ids
    assert set(ids) == {"qwen/qwen3-coder-next", "gemma/gemma-4-26b"}
    by = {m["id"]: m for m in out["models"]}
    assert by["qwen/qwen3-coder-next"]["active"] is True     # loaded
    assert by["gemma/gemma-4-26b"]["active"] is False        # configured, not loaded
    # active models sort first
    assert out["models"][0]["active"] is True
    assert out["current"] == "qwen/qwen3-coder-next"
    assert out["current_active"] is True


def test_chat_models_served_not_in_registry_still_listed(monkeypatch, tmp_path):
    api = _api(monkeypatch, tmp_path)
    monkeypatch.setattr(api, "_served_model_ids_for_role",
                        lambda role: {"mystery/loaded-model"})
    monkeypatch.setattr(api._acfg, "get", lambda role: {"provider": "local"})
    monkeypatch.setattr(api._acfg, "archetypes", lambda: ["chat"])
    import aiforge_core.config.model_registry as mr
    monkeypatch.setattr(mr, "list_models", lambda: [])

    out = api.chat_models()
    assert [m["id"] for m in out["models"]] == ["mystery/loaded-model"]
    assert out["models"][0]["active"] is True
