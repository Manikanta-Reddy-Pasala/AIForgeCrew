"""Unit tests for the live_verifier stage.

Covers:
  * Recipe loader picks the project-specific markdown when present
    and falls back to ``_default.md`` (then to an inline stub) when
    the file is missing.
  * ``adk_runner._extract_live_verifier`` parses the fenced ```json```
    block the agent is told to emit and survives extra prose around
    it; returns ``None`` cleanly on garbage / missing state.
"""
from __future__ import annotations

from pathlib import Path

import pytest


def test_recipe_loader_returns_project_specific_when_present() -> None:
    from aiforge_core.agents import live_verifier as lv

    md = lv.load_recipe("PosClientBackend")
    assert "PosClientBackend" in md
    assert "port-forward" in md
    assert "verdict" in md.lower()


def test_recipe_loader_returns_tally_recipe() -> None:
    from aiforge_core.agents import live_verifier as lv

    md = lv.load_recipe("TallyConnector")
    assert "TallyConnector" in md
    assert "live_handoff" in md


def test_recipe_loader_falls_back_to_default() -> None:
    from aiforge_core.agents import live_verifier as lv

    md = lv.load_recipe("NoSuchRepo_xyzzy")
    assert "generic" in md.lower() or "default" in md.lower()


def test_recipe_loader_handles_none_project() -> None:
    from aiforge_core.agents import live_verifier as lv

    md = lv.load_recipe(None)
    assert isinstance(md, str) and md


def test_extract_live_verifier_parses_fenced_json() -> None:
    from aiforge_core.runtime import adk_runner

    raw = (
        "Ran ./mvnw test, exit 0.\n\n"
        "```json\n"
        '{"ok": true, "rationale": "47 tests passed", "evidence": ["BUILD SUCCESS"]}\n'
        "```\n"
    )
    out = adk_runner._extract_live_verifier({"live_verifier_verdict": raw})
    assert out is not None
    assert out["ok"] is True
    assert "47 tests" in out["rationale"]


def test_extract_live_verifier_picks_last_block_when_multiple() -> None:
    from aiforge_core.runtime import adk_runner

    raw = (
        "First attempt:\n"
        "```json\n"
        '{"ok": false, "rationale": "interim"}\n'
        "```\n"
        "Retried, final:\n"
        "```json\n"
        '{"ok": true, "rationale": "final"}\n'
        "```\n"
    )
    out = adk_runner._extract_live_verifier({"live_verifier_verdict": raw})
    assert out is not None
    assert out["ok"] is True
    assert out["rationale"] == "final"


def test_extract_live_verifier_handles_raw_dict() -> None:
    from aiforge_core.runtime import adk_runner

    out = adk_runner._extract_live_verifier({
        "live_verifier_verdict": {"ok": False, "rationale": "boot timed out"},
    })
    assert out == {"ok": False, "rationale": "boot timed out"}


def test_extract_live_verifier_returns_none_for_garbage() -> None:
    from aiforge_core.runtime import adk_runner

    for v in (None, "", "no json here", "```not actually json```", 42):
        assert adk_runner._extract_live_verifier(
            {"live_verifier_verdict": v},
        ) is None


def test_extract_live_verifier_returns_none_when_missing() -> None:
    from aiforge_core.runtime import adk_runner

    assert adk_runner._extract_live_verifier({}) is None


# ── stage attribution ─────────────────────────────────────────────────


def test_resolve_stage_attribution_returns_model_provider() -> None:
    from aiforge_core.runtime import observability as o

    attr = o._resolve_stage_attribution("doer")
    assert attr["effective_provider"] in {"local", "claude_local",
                                          "ollama_cloud", "openrouter"}
    assert attr["model_configured"] is not None
    # No leading slash — long LM Studio paths are stripped to the leaf.
    assert not (attr["model_configured"] or "").startswith("/")


def test_resolve_stage_attribution_force_provider_wins(monkeypatch) -> None:
    from aiforge_core.runtime import observability as o, pipeline as p

    monkeypatch.setattr(p, "_FORCE_PROVIDER", "claude_local")
    attr = o._resolve_stage_attribution("doer")
    assert attr["effective_provider"] == "claude_local"
    assert attr["force_provider"] == "claude_local"


def test_resolve_stage_attribution_unknown_role_returns_safe_default() -> None:
    from aiforge_core.runtime import observability as o

    attr = o._resolve_stage_attribution("__not_a_role__")
    assert attr["effective_provider"] == "unknown"


# ── deploy recipe loader ──────────────────────────────────────────────


def test_load_deploy_recipe_posclient_has_tekton_steps() -> None:
    from aiforge_core.agents import live_verifier as lv

    md = lv.load_deploy_recipe("PosClientBackend")
    assert "tekton" in md.lower()
    assert "argocd" in md.lower()
    assert "AIFORGE_AUTO_MERGE" in md


def test_load_deploy_recipe_tally_emits_windows_handoff() -> None:
    from aiforge_core.agents import live_verifier as lv

    md = lv.load_deploy_recipe("TallyConnector")
    assert "windows" in md.lower()
    assert "AIFORGE_AUTO_MERGE" in md


def test_load_deploy_recipe_falls_back_to_default() -> None:
    from aiforge_core.agents import live_verifier as lv

    md = lv.load_deploy_recipe("NoSuchRepo")
    assert "AIFORGE_AUTO_MERGE" in md


def test_live_verifier_prompt_contains_both_sections() -> None:
    from aiforge_core.agents import live_verifier as lv

    prompt = lv._prompt_for_project("PosClientBackend")
    # Both placeholders resolved (no unfilled braces left).
    assert "{deploy_md}" not in prompt
    assert "{recipe_md}" not in prompt
    # Deploy section appears BEFORE Verify section in prompt order.
    assert prompt.index("### Deploy") < prompt.index("### Verify")
    # Both project recipes actually inlined.
    assert "tekton" in prompt.lower()
    assert "port-forward" in prompt.lower() or "pos-api.oneshell.in" in prompt
