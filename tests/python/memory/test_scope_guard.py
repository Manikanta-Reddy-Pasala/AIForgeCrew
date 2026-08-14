"""Global scope must be EARNED, and irrelevant global rules must not be injected.

A global learning is put in front of the model on every turn of every repo as a
mandatory rule. On a live install 16 of 36 global learnings named a specific
file (calc.py x7, demo.py x4) — benchmark artifacts — so every chat turn opened
with "All arithmetic functions in calc.py must include edge-case handling".
"""
from __future__ import annotations

import pytest

from aiforge_core.memory import scope_guard as sg


@pytest.mark.parametrize("text", [
    "All arithmetic functions in calc.py must include edge-case handling",
    "Controllers live in src/api/controllers",
    "call com.oneshell.OrderService.retry() before publishing",
    "update the values in config/settings.yaml",
])
def test_artifact_references_cannot_be_global(text):
    assert sg.may_be_global(text) is False
    assert sg.names_specific_artifact(text)
    assert sg.demote_reason(text)


@pytest.mark.parametrize("text", [
    "use && not ; to gate deploy commands",
    "always run the tests before committing",
    "prefer last-write-wins on conflicting updates",
    "cast lambda memory hints in Java",
])
def test_genuinely_universal_facts_stay_global(text):
    assert sg.may_be_global(text) is True
    assert sg.demote_reason(text) == ""


def test_a_bare_word_is_not_evidence():
    # "config"/"build" alone must not demote — only a concrete artifact does.
    assert sg.may_be_global("keep build config in version control") is True


# ── read side: tier 3 must not manufacture mandatory rules ───────────────────

def _node(body, title=""):
    return {"type": "learning", "id": body[:8], "body": body,
            "meta": {"title": title, "timestamp": "2026-01-01"}}


def test_rank_falls_back_to_recency_by_default():
    from aiforge_core.memory.okf.retrieve import _rank_by_query
    nodes = [_node("something about penguins")]
    # A repo card the agent needs regardless still comes back.
    assert _rank_by_query(nodes, "unrelated query text", 5) == nodes


def test_require_match_returns_nothing_when_irrelevant():
    from aiforge_core.memory.okf.retrieve import _rank_by_query
    nodes = [_node("something about penguins")]
    assert _rank_by_query(nodes, "kubernetes ingress", 5,
                          require_match=True) == []


def test_require_match_still_returns_real_matches():
    from aiforge_core.memory.okf.retrieve import _rank_by_query
    nodes = [_node("kubernetes ingress needs a tls secret"),
             _node("something about penguins")]
    out = _rank_by_query(nodes, "kubernetes ingress", 5, require_match=True)
    assert len(out) == 1 and "kubernetes" in out[0]["body"]


def test_require_match_keeps_everything_when_there_is_no_query():
    # An empty query is "no signal", not "nothing is relevant" — the caller
    # still gets context rather than an empty block.
    from aiforge_core.memory.okf.retrieve import _rank_by_query
    nodes = [_node("a"), _node("b")]
    assert _rank_by_query(nodes, "", 5, require_match=True) == nodes
