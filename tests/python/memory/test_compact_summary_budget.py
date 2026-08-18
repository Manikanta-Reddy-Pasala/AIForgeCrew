"""The brief summariser must size its calls from the window, not a constant.

`_SUMMARY_INPUT_CAP` was a flat 28,000 chars: too small for a 262k model (it
forced map-reduce passes over text that would have fitted in one call) and too
large the moment a role points at a 32k one — which is how a compaction ends up
failing on length and falling back to a deterministic merge for everything.
"""
from __future__ import annotations

import pytest

from aiforge_core.memory.md_store import _compact as C


@pytest.fixture()
def small_window(monkeypatch):
    monkeypatch.setenv("AIFORGE_CONSOLIDATE_CTX_TOKENS", "16000")


def test_the_cap_follows_the_roles_window(small_window):
    cap = C._summary_input_cap("learner")
    # 16k window − 4k completion − 2k slack ≈ 10k tokens ≈ 30k chars, so the
    # ceiling constant is what binds here…
    assert cap <= C._SUMMARY_INPUT_CAP
    assert cap >= 4000


def test_a_tiny_window_shrinks_the_cap_below_the_constant(monkeypatch):
    monkeypatch.setenv("AIFORGE_CONSOLIDATE_CTX_TOKENS", "8192")
    cap = C._summary_input_cap("learner")
    assert cap < C._SUMMARY_INPUT_CAP, "a small window must bind before the constant"


def test_an_oversized_block_is_split_not_sent_whole(monkeypatch, small_window):
    """One enormous capture used to go to the model whole — batching never split
    a single block — and a length 400 bailed the WHOLE compaction."""
    sizes = []

    def _fake(text, role):
        sizes.append(len(text))
        return "## Facts\n\n- something"

    monkeypatch.setattr(C, "_summarize_block", _fake)
    cap = C._summary_input_cap("learner")
    huge = "\n".join(f"line {i} " + "y" * 80 for i in range(2000))
    assert len(huge) > cap, "this test needs a block bigger than the cap"

    out = C._summarize_notes([huge], role="learner")

    assert out is not None
    assert sizes, "the summariser was never called"
    assert max(sizes) <= cap, f"a {max(sizes)}-char block was sent against a {cap} cap"


def test_a_block_over_the_cap_is_refused_rather_than_400ing(monkeypatch, small_window):
    """The guard inside the call itself: returning None routes the caller to a
    deterministic merge, where a provider 400 would have lost the content."""
    called = []
    monkeypatch.setattr("aiforge_core.llm.client.complete",
                        lambda *a, **k: called.append(1) or "x")

    cap = C._summary_input_cap("learner")
    assert C._summarize_block("z" * (cap + 1), "learner") is None
    assert called == [], "an oversized prompt was still sent"


def test_normal_sized_notes_still_go_in_one_call(monkeypatch, small_window):
    calls = []

    def _fake(text, role):
        calls.append(text)
        return "## Facts\n\n- ok"

    monkeypatch.setattr(C, "_summarize_block", _fake)

    C._summarize_notes(["a short note", "another short note"], role="learner")

    assert len(calls) == 1, "small input must not be split into a map-reduce"
