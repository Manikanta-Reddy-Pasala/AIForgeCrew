"""Per-archetype provider/model toggle round-trip + env precedence."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from aiforge_core.config import agent_config as ac


@pytest.fixture
def isolated_cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Each test gets a clean ~/.aiforge dir + cleared env overrides for
    every archetype to keep the persisted JSON the only source of state."""
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    for role in ac.archetypes():
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

    on_disk = json.loads((isolated_cfg / "agent_config.json").read_text())
    assert on_disk["doer"]["provider"] == "ollama_cloud"
    assert on_disk["doer"]["model"] == "qwen3-coder:480b"
    assert on_disk["doer"]["base_url"] is None


def test_set_role_rejects_legacy_archetype(isolated_cfg: Path) -> None:
    """Legacy archetypes (understander/grounder/validator/tester) were
    ripped in the v5 cleanup — config layer must reject them. Verifier
    was re-added as an opt-in plan critic."""
    for legacy in ("understander", "grounder", "validator", "tester",
                   "supervisor", "feedback_alias", "chat"):
        with pytest.raises(ValueError):
            ac.set_role(legacy, "local", "x")


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
    monkeypatch.setenv("AIFORGE_DOER_PROVIDER", "anthropic")
    monkeypatch.setenv("AIFORGE_DOER_MODEL", "claude-haiku-4-5-20251001")
    cfg = ac.get("doer")
    assert cfg["provider"] == "anthropic"
    assert cfg["model"] == "claude-haiku-4-5-20251001"


def test_list_providers(isolated_cfg: Path) -> None:
    ids = {p["id"] for p in ac.list_providers()}
    assert ids == {"local", "anthropic", "ollama_cloud", "claude_local"}
    for p in ac.list_providers():
        assert p["label"]
        assert p["default_model"]


def test_claude_local_round_trip(isolated_cfg: Path) -> None:
    ac.set_role("architect", "claude_local", "claude-opus-4-7")
    cfg = ac.get("architect")
    assert cfg["provider"] == "claude_local"
    assert cfg["model"] == "claude-opus-4-7"


def test_resolve_litellm_claude_local_marker(isolated_cfg: Path) -> None:
    ac.set_role("doer", "claude_local", "claude-opus-4-7")
    resolved = ac.resolve_litellm("doer")
    assert resolved["_claude_cli"] is True
    assert resolved["api_base"] == "claude:cli"
    assert resolved["model_id"] == "claude-opus-4-7"
    assert resolved["api_key"] == ""


def test_list_models_claude_local_curated(isolated_cfg: Path) -> None:
    models = ac.list_models("claude_local")
    ids = {m["id"] for m in models}
    assert "claude-opus-4-7" in ids
    assert "claude-sonnet-4-6" in ids


def test_apply_profile_claude_local(isolated_cfg: Path) -> None:
    out = ac.apply_profile("claude_local")
    for role in ac.archetypes():
        assert out[role]["provider"] == "claude_local"
        assert out[role]["model"] == "claude-opus-4-7"
        # Round-trip: read back from disk via fresh load_all().
        assert ac.get(role)["provider"] == "claude_local"


def test_apply_profile_ollama_cloud(isolated_cfg: Path) -> None:
    ac.apply_profile("ollama_cloud")
    for role in ac.archetypes():
        cfg = ac.get(role)
        assert cfg["provider"] == "ollama_cloud"
        assert cfg["model"] == "qwen3-coder:480b"


def test_apply_profile_local(isolated_cfg: Path) -> None:
    ac.apply_profile("local")
    for role in ac.archetypes():
        assert ac.get(role)["provider"] == "local"


def test_apply_profile_then_mix(isolated_cfg: Path) -> None:
    ac.apply_profile("local")
    ac.set_role("architect", "claude_local", "claude-opus-4-7")
    ac.set_role("doer", "ollama_cloud", "qwen3-coder:480b")
    assert ac.get("architect")["provider"] == "claude_local"
    assert ac.get("doer")["provider"] == "ollama_cloud"
    assert ac.get("planner")["provider"] == "local"


def test_verifier_configurable(isolated_cfg: Path) -> None:
    """Verifier was re-added as opt-in plan critic — config layer must
    treat it like any other archetype."""
    ac.set_role("verifier", "claude_local", "claude-opus-4-7")
    cfg = ac.get("verifier")
    assert cfg["provider"] == "claude_local"
    assert cfg["model"] == "claude-opus-4-7"


def test_apply_profile_includes_verifier(isolated_cfg: Path) -> None:
    out = ac.apply_profile("local")
    assert "verifier" in out
    assert out["verifier"]["provider"] == "local"


def test_apply_profile_unknown_raises(isolated_cfg: Path) -> None:
    with pytest.raises(ValueError):
        ac.apply_profile("nope")


def test_profiles_listed(isolated_cfg: Path) -> None:
    names = set(ac.profiles())
    assert {"claude_local", "ollama_cloud", "local"} <= names


def test_archetypes_v5_pipeline(isolated_cfg: Path) -> None:
    arches = ac.archetypes()
    assert len(arches) == 6
    assert set(arches) == {
        "architect", "planner", "verifier", "doer", "feedback", "learner",
    }
    # Order matters: verifier sits between planner and doer.
    assert arches[0] == "architect"
    assert arches[2] == "verifier"
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
