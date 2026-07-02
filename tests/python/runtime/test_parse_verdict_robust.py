"""Fix 2 — _parse_verdict must not fail-OPEN on prose-wrapped verdicts.

The old code took ``text.split()[0]`` (FIRST WORD only), so a local model
emitting ``I reject this because {"verdict":"reject"}`` parsed to ``"i"`` →
not reject → verifier ships the bad plan / validator skips the replan.
Both gates fail-open. Harden with brace-balanced extraction + a
known-verdict-word scan that fails SAFE on ambiguity."""
from __future__ import annotations

import asyncio

import pytest

from aiforge_core.runtime import graph_pipeline as gp


class _FakeCtx:
    def __init__(self, state: dict) -> None:
        self.state = state
        self.route = None


def _run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize("raw,expected", [
    # prose wrapping an embedded JSON object → brace-balanced extraction.
    ('I reject this because {"verdict":"reject"}', "reject"),
    # fenced JSON (backward compatible clean path).
    ('```json\n{"verdict":"approve"}\n```', "approve"),
    # prose fail with NO json → bare-token scan finds the verdict word.
    ('I think this should fail', "fail"),
    # prose containing both a reject-shaped token AND pass → negative wins.
    ('do not pass this, reject it', "reject"),
    # clean bare token (backward compatible).
    ('pass', "pass"),
    ('approve', "approve"),
    # dict passthrough.
    ({"verdict": "request_changes"}, "request_changes"),
    # genuine garbage → documented default (None).
    ('asdf qwer zxcv', None),
    ('', None),
])
def test_parse_verdict_robust(raw, expected) -> None:
    assert gp._parse_verdict(raw) == expected


def test_verifier_gate_routes_replan_on_prose_reject() -> None:
    # A prose-wrapped reject must route VERIFY_REPLAN, not VERIFY_PASS.
    state = {"verifier_verdict": 'I reject this plan {"verdict":"reject"}'}
    ctx = _FakeCtx(state)
    _run(gp._verifier_gate(ctx))
    assert ctx.route == gp.ROUTE_VERIFY_REPLAN


def test_validator_failed_on_prose_reject() -> None:
    assert gp._validator_failed(
        {"validator_verdict": 'I reject {"verdict":"reject"}'}) is True


def test_feedback_not_passed_on_prose_fail() -> None:
    assert gp._feedback_passed(
        {"feedback_verdict": 'this must fail {"verdict":"fail"}'}) is False
