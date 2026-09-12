"""``ENHANCE_BLOCKED`` must mean "I cannot build this", not "this is fine".

Found by a live team run: the Enhancer answered Rule 2 ("the body is already in
shape — return it") using Rule 3's abort line, and every reader keyed on the
bare prefix. The run stopped after 55 seconds, wrote no files, and reported

    "I need more detail before I can build this — The user's request is already
     in a complete, actionable form … no enhancement is needed …"

Three readers share the contract (the chat pipeline, the ticket runner's
verdict, and the ADK guard), so the predicate lives with the prompt.
"""
from __future__ import annotations

import types

from aiforge_core.runtime.prompts.enhancer import block_reason

# The exact sentence the 55-second team run produced.
SPURIOUS = ("ENHANCE_BLOCKED: The user's request is already in a complete, "
            "actionable form matching the required output format; no "
            "enhancement is needed — it meets all rules and contains clear, "
            "checkable acceptance criteria with no ambiguity.")
GENUINE = "ENHANCE_BLOCKED: no goal is extractable — the body is one word"


def test_a_sentinel_that_says_nothing_is_wrong_is_not_a_refusal():
    assert block_reason(SPURIOUS) is None


def test_a_real_refusal_still_stops_the_run():
    assert block_reason(GENUINE).startswith("no goal is extractable")


def test_a_bare_sentinel_is_still_a_refusal():
    """No reason given is not permission to carry on — it is a refusal with a
    default reason, which is how the contract read before this fix."""
    assert block_reason("ENHANCE_BLOCKED")
    assert block_reason("ENHANCE_BLOCKED:")


def test_an_ordinary_enhanced_body_is_never_a_refusal():
    assert block_reason("## Goal\n\nBuild the CLI\n\n## Acceptance\n- tests pass") is None
    assert block_reason("") is None


def test_the_chat_pipeline_reads_the_same_contract():
    from aiforge_core.runtime import chat_pipeline as cp
    ev = {"type": "thought", "role": "enhancer", "text": SPURIOUS}
    assert cp._enhancer_block_reason(ev) is None
    assert cp._enhancer_block_reason({**ev, "text": GENUINE})
    # …and only the Enhancer's own thoughts are read as the sentinel at all
    assert cp._enhancer_block_reason({**ev, "role": "doer", "text": GENUINE}) is None


def test_the_ticket_runner_reads_the_same_contract():
    from aiforge_core.runtime.adk_runner import _verdict
    assert _verdict._enhancer_block_reason({"enhanced_body": SPURIOUS}) is None
    assert _verdict._enhancer_block_reason({"enhanced_body": GENUINE})
    assert _verdict._enhancer_block_reason({"enhanced_body": None}) is None


def _guard_state(body: str) -> dict:
    from aiforge_core.runtime.pipeline import _make_enhancer_guard
    state = {"raw_ask": "build a todo CLI with tests", "enhanced_body": body}
    _make_enhancer_guard()(callback_context=types.SimpleNamespace(state=state))
    return state


def test_a_spurious_sentinel_never_becomes_the_doers_brief():
    """Ignoring the block is only safe if the brief is repaired too — otherwise
    the Doer builds against the sentence instead of the request."""
    assert _guard_state(SPURIOUS)["enhanced_body"] == "build a todo CLI with tests"


def test_a_real_refusal_is_left_for_the_runner():
    assert _guard_state(GENUINE)["enhanced_body"] == GENUINE
