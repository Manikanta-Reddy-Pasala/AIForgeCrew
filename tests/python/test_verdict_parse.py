"""Tests for the Feedback-verdict parser in :mod:`adk_runner`.

The parser is the bridge between the Feedback agent's free-text output
and the ticket-status mapping. It needs to handle three real-world
shapes the model emits:

* leading-token plain text (the new prompt asks for this)
* legacy strict JSON ``{"verdict": "pass", ...}`` (some models still
  output this even with the new prompt)
* prose-wrapped garbage from chatty models that used to
  parse-fail and silently drop into ``fail`` (ONE-107 root cause)
"""
from __future__ import annotations

import pytest

from aiforge_core.runtime.adk_runner import _extract_verdict


def test_leading_token_pass() -> None:
    assert _extract_verdict({"feedback_verdict": "pass"}) == "pass"


def test_leading_token_with_rationale() -> None:
    txt = "pass\nDoer wrote the file and mvn compile returned 0."
    assert _extract_verdict({"feedback_verdict": txt}) == "pass"


def test_leading_token_fail() -> None:
    assert _extract_verdict({"feedback_verdict": "fail\nno test added"}) == "fail"


def test_scope_violation_outranks_fail_substring() -> None:
    """``scope_violation`` literally contains ``fail`` as a substring;
    the parser MUST check the longer token first."""
    out = _extract_verdict({"feedback_verdict": "scope_violation\nedited /etc/passwd"})
    assert out == "scope_violation"


def test_legacy_json_dict_shape() -> None:
    obj = {"verdict": "pass", "rationale": "ok"}
    assert _extract_verdict({"feedback_verdict": obj}) == "pass"


def test_legacy_json_string_shape() -> None:
    raw = '{"verdict": "fail", "rationale": "missing tests"}'
    assert _extract_verdict({"feedback_verdict": raw}) == "fail"


def test_prose_wrapping_falls_back_to_fail() -> None:
    """When the model returns prose with no leading token, the
    parser bails to ``fail`` rather than silently shipping a wrong
    verdict — caller can still ship the PR via the changes-on-disk
    gate."""
    txt = "I think the work looks reasonable but I'd want to double-check..."
    assert _extract_verdict({"feedback_verdict": txt}) == "fail"


def test_markdown_wrapped_token() -> None:
    """Some models markdown-wrap the leading token despite the rules."""
    assert _extract_verdict({"feedback_verdict": "**pass**\nlooks good"}) == "pass"


def test_uppercase_token_normalised() -> None:
    assert _extract_verdict({"feedback_verdict": "PASS"}) == "pass"


def test_missing_verdict_defaults_to_fail() -> None:
    assert _extract_verdict({}) == "fail"
    assert _extract_verdict({"feedback_verdict": None}) == "fail"
    assert _extract_verdict({"feedback_verdict": ""}) == "fail"


def test_empty_dict_falls_back() -> None:
    assert _extract_verdict({"feedback_verdict": {}}) == "fail"
