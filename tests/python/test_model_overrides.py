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


def test_apply_skips_suffix_and_cap_for_generative_roles():
    # The 2500-cap / anti-think recipe is for short judges; applying it to
    # the doer truncates file-write tool-call args. Generative roles must NOT
    # get the suffix or the cap — but the benign temperature still applies.
    for role in ("doer", "planner", "refiner", "enhancer", "architect",
                 "learner", "researcher", "DOER"):
        req = _req(system="You are the doer.", max_out=16384)
        out = mo.apply("qwen3.5-122b", req, role=role)
        assert out.config.max_output_tokens == 16384, role   # cap NOT applied
        assert mo.NO_REASONING_SUFFIX not in str(
            out.config.system_instruction), role             # suffix NOT applied
        assert out.config.temperature == 0.1, role           # temp IS applied


def test_apply_skips_entirely_for_generative_without_temp(monkeypatch, tmp_path):
    # An override with no temperature key has nothing benign for a generative
    # role -> request returned untouched (identity preserved).
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    (tmp_path / "model_overrides.json").write_text(json.dumps({
        "cap-only-model": {"max_output_tokens": 1000},
    }))
    req = _req(system="r", max_out=16384)
    out = mo.apply("cap-only-model", req, role="doer")
    assert out is req
    assert out.config.max_output_tokens == 16384


def test_qwythos_forces_temperature_zero_all_roles():
    assert mo.lookup("qwythos-9b-claude-mythos-5-1m-mxfp8-mlx")["temperature"] == 0.0
    # Applies on both a judge role and a generative role.
    for role in ("triage", "planner", "enhancer", "architect"):
        out = mo.apply("qwythos-9b-claude-mythos-5-1m-mxfp8-mlx",
                       _req(max_out=16384), role=role)
        assert out.config.temperature == 0.0, role
        assert out.config.max_output_tokens == 16384, role   # no cap leak


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
