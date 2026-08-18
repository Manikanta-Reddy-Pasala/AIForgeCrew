"""A fold must fit the model's context window — both halves of it.

From the live failure on 2026-08-18, folding a 2,163-node mesh:

    ContextWindowExceededError: maximum context length is 262144 tokens.
    However, you requested 32768 output tokens and your prompt contains at
    least 229377 input tokens, for a total of at least 262145 tokens.

Over by ONE token, and every retry failed identically because nothing shrank:
the accumulated sections were sent whole on every chunk, and the output request
was sized from that same growing payload.
"""
from __future__ import annotations

import json

import pytest

from aiforge_core.runtime.work_notes import _consolidate as C


@pytest.fixture()
def small_window(monkeypatch):
    """A 20k-token window — big enough to be realistic, small enough to trip."""
    monkeypatch.setenv("AIFORGE_CONSOLIDATE_CTX_TOKENS", "20000")
    return 20000


def _big_state(n_facts=4000, width=120):
    return {"objective": "keep the mesh distilled",
            "key_results": [], "links": [], "learnings": [],
            "facts": [f"fact {i:05d} " + "x" * width for i in range(n_facts)]}


# ── the output side ───────────────────────────────────────────────────────

def test_the_output_request_is_cut_to_what_the_window_has_left(small_window, monkeypatch):
    seen = {}

    def _fake(role, msgs, model, **kw):
        seen["max_tokens"] = kw.get("max_tokens")
        seen["input_tokens"] = C._est_tokens(msgs[-1]["content"])

        class _R:
            objective = "o"; key_results = []; facts = ["f"]; links = []; learnings = []
        return _R()

    monkeypatch.setattr("aiforge_core.llm.structured.structured_complete", _fake)
    state = {"objective": "o", "key_results": [], "links": [], "learnings": [],
             "facts": [f"fact {i} " + "y" * 100 for i in range(300)]}

    C._consolidate_once(state, "new information here", "learner")

    assert seen["max_tokens"] >= C._MIN_OUTPUT_TOKENS
    total = seen["input_tokens"] + seen["max_tokens"]
    assert total <= small_window, f"asked for {total} tokens of a {small_window} window"


def test_a_prompt_with_no_room_left_never_calls_the_model(small_window, monkeypatch):
    """The exact shape of the incident: a prompt so large that any completion
    would overflow. Calling would 400 every time, so it must not be attempted."""
    called = []
    monkeypatch.setattr("aiforge_core.llm.structured.structured_complete",
                        lambda *a, **k: called.append(1))

    out = C._consolidate_once(_big_state(), "more text", "learner")

    assert called == [], "a doomed call was still made"
    # …and no information was lost: the deterministic merge kept everything.
    assert len(out["facts"]) >= 4000


# ── the input side ────────────────────────────────────────────────────────

def test_the_accumulated_state_is_capped_per_call(small_window, monkeypatch):
    """The runaway: state grew with every chunk until the prompt no longer fit."""
    sizes = []

    def _fake(role, msgs, model, **kw):
        payload = json.loads(msgs[-1]["content"])
        sizes.append(len(json.dumps(payload["current_sections"])))

        class _R:
            objective = "o"; key_results = []; links = []; learnings = []
            facts = payload["current_sections"].get("facts", []) + ["new fact"]
        return _R()

    monkeypatch.setattr("aiforge_core.llm.structured.structured_complete", _fake)
    monkeypatch.setenv("AIFORGE_CONSOLIDATE_STATE_CHARS", "4000")

    C.consolidate(_big_state(n_facts=200), "text " * 6000, role="learner")

    assert sizes, "the model was never called"
    assert max(sizes) <= 6000, f"state sent uncapped: {max(sizes)} chars"


def test_facts_held_back_from_the_call_always_survive(small_window, monkeypatch):
    """The contract that makes capping safe.

    A consolidating model MAY drop what it was shown — merging and superseding
    is its job. It must never cost us what it was NEVER shown: those items are
    held back precisely because they did not fit, so they are re-unioned
    locally. This stub is maximally destructive (it returns one fact and
    discards everything it saw) to prove the held half is not at its mercy.
    """
    seen = []

    def _fake(role, msgs, model, **kw):
        seen.extend(json.loads(msgs[-1]["content"])["current_sections"]["facts"])

        class _R:
            objective = "o"; key_results = []; links = []; learnings = []
            facts = ["a brand new fact"]
        return _R()

    monkeypatch.setattr("aiforge_core.llm.structured.structured_complete", _fake)
    monkeypatch.setenv("AIFORGE_CONSOLIDATE_STATE_CHARS", "2000")
    state = _big_state(n_facts=500)

    out = C.consolidate(state, "some new text", role="learner")

    kept = set(out["facts"])
    assert "a brand new fact" in kept
    never_shown = [f for f in state["facts"] if f not in set(seen)]
    assert never_shown, "this test needs facts that were held back"
    lost = [f for f in never_shown if f not in kept]
    assert not lost, f"{len(lost)} fact(s) the model never saw were still lost"


def test_a_well_behaved_model_loses_nothing_at_all(small_window, monkeypatch):
    """The realistic case: a model that returns what it was given plus the new
    item. Capping the call must then be invisible — every fact comes back."""
    def _fake(role, msgs, model, **kw):
        cs = json.loads(msgs[-1]["content"])["current_sections"]

        class _R:
            objective = cs.get("objective") or ""
            key_results = cs.get("key_results") or []
            links = cs.get("links") or []
            learnings = cs.get("learnings") or []
            facts = (cs.get("facts") or []) + ["a brand new fact"]
        return _R()

    monkeypatch.setattr("aiforge_core.llm.structured.structured_complete", _fake)
    monkeypatch.setenv("AIFORGE_CONSOLIDATE_STATE_CHARS", "2000")
    state = _big_state(n_facts=500)

    out = C.consolidate(state, "some new text", role="learner")

    kept = set(out["facts"])
    missing = [f for f in state["facts"] if f not in kept]
    assert not missing, f"{len(missing)} fact(s) lost while capping the call"
    assert "a brand new fact" in kept


def test_split_state_round_trips_every_item(small_window):
    state = _big_state(n_facts=50)
    state["learnings"] = [f"learning {i}" for i in range(20)]
    sent, held = C._split_state(state, 1500)

    assert C._sections_chars(sent) <= 2000
    for k in ("facts", "learnings"):
        assert sorted(sent[k] + held[k]) == sorted(state[k])
