"""Validator carries the test-DEPTH (edge-coverage) axis.

Lever #1 of the logic-bug gap: a happy-path-only test suite lets a bug
in an untested branch (off-by-one, wrong return, missing advance) pass.
The Validator must judge test depth, not just presence, and route
``request_changes`` (which validator_gate replans on) when tests are
shallow. Imported from the module directly to stay google-adk-free.
"""
from __future__ import annotations

from aiforge_core.runtime.prompts.validator import VALIDATOR


def test_contract_has_cover_edges_field():
    assert "tests_cover_edges" in VALIDATOR
    # distinct from mere presence
    assert "tests_present" in VALIDATOR


def test_rule_demands_edge_cases_and_request_changes():
    low = VALIDATOR.lower()
    # names concrete edge categories so the judge knows what "depth" means
    assert "happy path" in low
    assert "boundaries" in low or "boundary" in low
    assert any(k in low for k in ("invalid", "error input", "error/invalid"))
    # shallow tests must drive the replan verdict
    assert "request_changes" in VALIDATOR


def test_pure_refactor_is_exempt():
    # must not force coverage on no-behaviour-change diffs / fast-path
    low = VALIDATOR.lower()
    assert "refactor" in low
    assert "exempt" in low
