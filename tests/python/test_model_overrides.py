"""Per-model request customization registry."""
from __future__ import annotations

import json

import pytest
from google.adk.models.llm_request import LlmRequest
from google.genai import types

from aiforge_core.config import model_overrides as mo


def _req(system: str | None = None, max_out: int | None = None):
    return LlmRequest(
        model="x",
        contents=[],
        config=types.GenerateContentConfig(
            system_instruction=system, max_output_tokens=max_out),
    )


def test_lookup_matches_substring_case_insensitive():
    assert mo.lookup("NEX-N2-MINI-nvfp4") is not None
    assert mo.lookup("openai/nex-n2-mini-nvfp4") is not None
    assert mo.lookup("qwen/qwen3-coder-next") is None
    assert mo.lookup(None) is None


def test_apply_appends_system_suffix():
    req = mo.apply("nex-n2-mini-nvfp4", _req(system="You are a judge."))
    assert "You are a judge." in str(req.config.system_instruction)
    assert mo.NO_REASONING_SUFFIX in str(req.config.system_instruction)


def test_apply_is_idempotent_on_suffix():
    once = mo.apply("nex-n2-mini-nvfp4", _req(system="r"))
    twice = mo.apply("nex-n2-mini-nvfp4", once)
    assert str(twice.config.system_instruction).count(
        "internal thinking is forbidden") == 1


def test_apply_caps_tokens_but_keeps_smaller_caller_cap():
    capped = mo.apply("nex-n2-mini-nvfp4", _req(max_out=None))
    assert capped.config.max_output_tokens == 2500
    smaller = mo.apply("nex-n2-mini-nvfp4", _req(max_out=900))
    assert smaller.config.max_output_tokens == 900
    bigger = mo.apply("nex-n2-mini-nvfp4", _req(max_out=16384))
    assert bigger.config.max_output_tokens == 2500


def test_apply_untouched_for_unknown_model():
    req = _req(system="r", max_out=16384)
    out = mo.apply("qwen/qwen3-coder-next", req)
    assert out is req


def test_apply_skips_generative_roles():
    # The 2500-cap / anti-think recipe is for short judges; applying it to
    # the doer truncates file-write tool-call args. Generative roles must
    # pass through untouched even on an overridden model.
    for role in ("doer", "planner", "refiner", "enhancer", "architect",
                 "learner", "researcher", "DOER"):
        req = _req(system="You are the doer.", max_out=16384)
        out = mo.apply("qwen3.5-122b", req, role=role)
        assert out is req, role
        assert out.config.max_output_tokens == 16384, role


def test_apply_still_applies_to_judge_roles():
    out = mo.apply("qwen3.5-122b", _req(max_out=None), role="triage")
    assert out.config.max_output_tokens == 2500
    assert mo.NO_REASONING_SUFFIX in str(out.config.system_instruction)


def test_file_overrides_merge(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    (tmp_path / "model_overrides.json").write_text(json.dumps({
        "my-custom-model": {"max_output_tokens": 1234},
        "nex-n2-mini": {"max_output_tokens": 999},  # wins over builtin
    }))
    assert mo.lookup("my-custom-model-v2")["max_output_tokens"] == 1234
    assert mo.lookup("nex-n2-mini-nvfp4")["max_output_tokens"] == 999


def test_original_request_never_mutated():
    req = _req(system="r", max_out=None)
    mo.apply("nex-n2-mini-nvfp4", req)
    assert req.config.max_output_tokens is None
    assert str(req.config.system_instruction) == "r"
