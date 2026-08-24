"""Tests for ``aiforge_core.runtime.prompts_extended``."""
from __future__ import annotations

import pytest

from aiforge_core.runtime import prompts_extended as pe


def test_all_prompts_are_nonempty_strings():
    for name in ("TRIAGE", "RESEARCHER", "REFINER"):
        v = getattr(pe, name)
        assert isinstance(v, str)
        assert len(v) > 50, f"{name} should be a meaningful prompt, got {len(v)} chars"


def test_triage_demands_strict_json():
    assert "STRICT JSON" in pe.TRIAGE
    assert "complexity" in pe.TRIAGE
    assert "trivial" in pe.TRIAGE
    assert "moderate" in pe.TRIAGE
    assert "hard" in pe.TRIAGE


def test_researcher_lists_read_only_tools():
    """Researcher must NOT mention any write tools."""
    assert "graphify_lookup" in pe.RESEARCHER
    assert "memory_lookup" in pe.RESEARCHER
    assert "file_read" in pe.RESEARCHER
    assert "file_write" not in pe.RESEARCHER
    assert "file_patch" not in pe.RESEARCHER
    assert "code_run" not in pe.RESEARCHER


def test_refiner_forbids_signature_changes():
    """Behaviour-neutral edits only — sanity check the prompt language."""
    assert "Forbidden" in pe.REFINER
    assert "signature" in pe.REFINER.lower() or "return type" in pe.REFINER.lower()
    assert "skipped" in pe.REFINER


def test_module_exports_are_stable():
    assert set(pe.__all__) == {
        "TRIAGE", "RESEARCHER", "REFINER",
        # 2026-06-11: context-gatherer prompts for the ParallelAgent stage.
        "CTX_MEMORY", "CTX_REPOMAP", "CTX_CONVENTIONS",
    }
