"""Per-archetype provider/model toggle round-trip + env precedence."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from aiforge_core.runtime import agent_config as ac


@pytest.fixture
def isolated_cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Each test gets a clean ~/.aiforge dir + cleared env overrides for
    every archetype to keep the persisted JSON the only source of state."""
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    for role in ac.archetypes() + ["supervisor", "feedback", "chat"]:
        for suffix in ("PROVIDER", "MODEL", "BASE_URL"):
            monkeypatch.delenv(f"AIFORGE_{role.upper()}_{suffix}",
                               raising=False)
    monkeypatch.delenv("AIFORGE_LM_BASE_URL", raising=False)
    monkeypatch.delenv("AIFORGE_OLLAMA_CLOUD_BASE_URL", raising=False)
    return tmp_path


def test_set_role_round_trip(isolated_cfg: Path) -> None:
    ac.set_role("doer", "ollama_cloud", "qwen3-coder:480b")
    cfg = ac.get("doer")
    assert cfg["provider"] == "ollama_cloud"
    assert cfg["model"] == "qwen3-coder:480b"
    assert cfg["base_url"] is None

    # Persisted file shape — disk format must include base_url field.
    on_disk = json.loads((isolated_cfg / "agent_config.json").read_text())
    assert on_disk["doer"]["provider"] == "ollama_cloud"
    assert on_disk["doer"]["model"] == "qwen3-coder:480b"
    assert on_disk["doer"]["base_url"] is None


def test_base_url_persists_and_surfaces_in_resolve(isolated_cfg: Path) -> None:
    custom = "http://192.168.70.50:1234/v1"
    ac.set_role("planner", "local", "Qwen3.6-27B-MLX-4bit", base_url=custom)
    cfg = ac.get("planner")
    assert cfg["base_url"] == custom

    resolved = ac.resolve_litellm("planner")
    # Local provider should prefer the stored base_url over the default.
    assert resolved["api_base"] == custom


def test_env_override_wins_over_persisted(
    isolated_cfg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ac.set_role("doer", "ollama_cloud", "qwen3-coder:480b")
    # Now flip via env — should be reflected in the next read.
    monkeypatch.setenv("AIFORGE_DOER_PROVIDER", "anthropic")
    monkeypatch.setenv("AIFORGE_DOER_MODEL", "claude-haiku-4-5-20251001")
    cfg = ac.get("doer")
    assert cfg["provider"] == "anthropic"
    assert cfg["model"] == "claude-haiku-4-5-20251001"


def test_list_providers_only_public(isolated_cfg: Path) -> None:
    ids = {p["id"] for p in ac.list_providers()}
    assert ids == {"local", "anthropic", "ollama_cloud"}
    for p in ac.list_providers():
        assert p["label"]
        assert p["default_model"]


def test_archetypes_returns_exactly_nine(isolated_cfg: Path) -> None:
    arches = ac.archetypes()
    assert len(arches) == 9
    assert set(arches) == {
        "understander", "planner", "verifier", "grounder",
        "doer", "validator", "tester", "architect", "learner",
    }
    # Order matters for the UI list — pin it.
    assert arches[0] == "understander"
    assert arches[-1] == "learner"


def test_list_models_unknown_provider_raises(isolated_cfg: Path) -> None:
    with pytest.raises(ValueError):
        ac.list_models("does-not-exist")


def test_list_models_anthropic_curated(isolated_cfg: Path) -> None:
    # Anthropic is fully static — no network — so this is deterministic.
    models = ac.list_models("anthropic")
    ids = {m["id"] for m in models}
    assert "claude-opus-4-7" in ids
    assert "claude-sonnet-4-6" in ids
    assert "claude-haiku-4-5-20251001" in ids


def test_set_role_rejects_bad_inputs(isolated_cfg: Path) -> None:
    with pytest.raises(ValueError):
        ac.set_role("not-a-role", "local", "x")
    with pytest.raises(ValueError):
        ac.set_role("doer", "not-a-provider", "x")
    with pytest.raises(ValueError):
        ac.set_role("doer", "local", "   ")
