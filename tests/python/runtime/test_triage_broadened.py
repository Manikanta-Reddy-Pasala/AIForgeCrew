"""Change 1 — broadened triage fast-path.

A small local triage model rarely emits the exact token ``trivial``; it
says ``simple`` / ``low`` / ``easy`` / ``minor`` / ``Trivial.`` /
`` minor `` (padded). The fast-path gate must recognise those synonyms
(after normalising case/whitespace/punctuation) yet still route a
genuinely complex ticket to the FULL path — never the reverse.
"""
from __future__ import annotations

import asyncio

import pytest

from aiforge_core.runtime import graph_pipeline as gp


class _FakeCtx:
    def __init__(self, state: dict) -> None:
        self.state = state
        self.route = None


def _route(state: dict) -> str:
    ctx = _FakeCtx(state)
    asyncio.run(gp._triage_gate(ctx))
    return ctx.route


@pytest.mark.parametrize("verdict", [
    "trivial", "simple", "low", "easy", "minor", "small",
    "Trivial.", " simple ", " minor ", "SIMPLE", "**easy**",
])
def test_trivial_synonyms_route_fast_path(verdict) -> None:
    assert _route({"complexity": verdict}) == gp.ROUTE_TRIVIAL


@pytest.mark.parametrize("verdict", [
    "moderate", "high", "complex", "hard", "medium", "difficult",
    "garbage", "", "unknown",
])
def test_nontrivial_routes_full(verdict) -> None:
    assert _route({"complexity": verdict}) == gp.ROUTE_FULL


def test_triage_verdict_bare_string_synonym() -> None:
    # model emitted just the word, not JSON
    assert _route({"triage_verdict": "simple"}) == gp.ROUTE_TRIVIAL
    assert _route({"triage_verdict": " Easy. "}) == gp.ROUTE_TRIVIAL


def test_triage_verdict_bare_garbage_defaults_full() -> None:
    assert _route({"triage_verdict": "junk"}) == gp.ROUTE_FULL


def test_triage_verdict_prose_wrapped_json() -> None:
    assert _route({"triage_verdict": 'Here is: {"complexity": "easy"}'}) \
        == gp.ROUTE_TRIVIAL


def test_strict_env_restores_exact_trivial(monkeypatch) -> None:
    monkeypatch.setenv("AIFORGE_TRIAGE_STRICT", "1")
    assert _route({"complexity": "simple"}) == gp.ROUTE_FULL
    assert _route({"complexity": "trivial"}) == gp.ROUTE_TRIVIAL
