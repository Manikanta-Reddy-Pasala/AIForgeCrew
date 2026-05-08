"""Tests for the new Architect structural-plan prompt + module wiring.

The Architect is an EXTERNAL Claude Code session — there's no live
``LlmAgent`` to drive in CI. We assert the prompt's machine-readable
contract instead: tree / symbols / imports keys, JSON shape, and the
fact that the per-archetype module re-exports it correctly so the
runtime can inject ``state['structural_plan']`` for the Doer.
"""
from __future__ import annotations

from aiforge_core import agents as agents_pkg
from aiforge_core.agents import architect as architect_mod
from aiforge_core.runtime import prompts


# ─── prompt contract ──────────────────────────────────────────────────


def test_architect_prompt_present_and_nontrivial():
    text = prompts.ARCHITECT
    assert isinstance(text, str)
    # ~500-char floor is generous; the real prompt is ~1200 chars.
    assert len(text) > 400, (
        f"architect prompt looks empty/stub ({len(text)} chars)")


def test_architect_prompt_documents_top_level_keys():
    """The structural plan MUST advertise tree / symbols / imports —
    those are what the Doer reads."""
    text = prompts.ARCHITECT
    for key in ("tree", "symbols", "imports"):
        assert key in text, (
            f"architect prompt missing top-level key: {key!r}")


def test_architect_prompt_demands_strict_json():
    """Loose prose output makes the Doer hallucinate. Prompt must ask
    for STRICT JSON."""
    text = prompts.ARCHITECT
    assert "STRICT JSON" in text or "strict json" in text.lower(), (
        "architect prompt must demand STRICT JSON output")


def test_architect_prompt_describes_canonical_owner_rule():
    """The whole point of the structural plan is canonical symbol
    ownership — the rule MUST be in the prompt."""
    text = prompts.ARCHITECT.lower()
    assert "canonical" in text or "owner" in text or "owns" in text, (
        "architect prompt must describe canonical-owner semantics")


def test_architect_prompt_rejects_phantom_paths():
    """Every value in ``symbols`` must resolve to ``tree`` or an
    existing file — the prompt must enforce this."""
    text = prompts.ARCHITECT.lower()
    assert "phantom" in text or (
        "tree" in text and "existing" in text), (
        "architect prompt must forbid phantom paths in symbols map")


def test_architect_prompt_describes_per_file_import_allowlist():
    """``imports`` is per-file, restricting what each file may import."""
    text = prompts.ARCHITECT.lower()
    assert "import" in text and "allowlist" in text, (
        "architect prompt must describe per-file import allowlist")


# ─── per-archetype module surface ─────────────────────────────────────


def test_architect_module_exports_prompt():
    """Old behaviour: ``architect.PROMPT`` was empty string. New
    behaviour: it surfaces the structural-plan contract."""
    assert architect_mod.PROMPT == prompts.ARCHITECT
    assert architect_mod.PROMPT != ""


def test_architect_module_output_key_is_structural_plan():
    """The runtime injects ``state['structural_plan']`` from the
    Architect's output. The output_key MUST match that name."""
    assert architect_mod.OUTPUT_KEY == "structural_plan"


def test_architect_build_still_returns_none():
    """Architect remains EXTERNAL — there's no ADK LlmAgent to build."""
    assert architect_mod.build(lambda role: None) is None


def test_architect_role_resolves_in_yaml():
    contracts = agents_pkg.load_agents()
    assert "architect" in contracts


def test_prompts_init_re_exports_architect():
    """Back-compat: ``from aiforge_core.runtime import prompts``
    surface MUST list ARCHITECT alongside the other archetypes."""
    assert "ARCHITECT" in prompts.__all__
    assert hasattr(prompts, "ARCHITECT")


# ─── shape sanity (machine-readable contract) ────────────────────────


def test_prompt_describes_dict_shape_for_symbols():
    """``symbols`` is a dict, not a list — accidentally describing
    it as a list would let models emit the wrong shape."""
    text = prompts.ARCHITECT
    # the example block shows symbols as ``{...}``.
    # Crude check: the symbols line uses curly braces, not square.
    sym_line_idx = text.find("\"symbols\"")
    assert sym_line_idx >= 0
    after = text[sym_line_idx: sym_line_idx + 80]
    assert "{" in after, (
        f"symbols example must use dict-shape ``{{...}}`` — saw: {after!r}")
