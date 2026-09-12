"""Background traffic is throttled less than chat — it must not also be unseen.

``compaction_rpm`` now defaults to 0 ("use whatever chat leaves of the global
ceiling"). With no global ceiling set either, a memory/compaction send was
unbounded on BOTH axes, so the limiter returned before recording it: the send
left the box and the toolbar meter never knew. That is the "uncapped AND
invisible" defect ``govern_send`` exists to prevent, reintroduced through a
default. Chat is deliberately unchanged: an operator who asks for no ceiling at
all keeps no window.
"""
from __future__ import annotations

import pytest

from aiforge_core.llm import rate_limiter as rl


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    """The in-process window, isolated — same shape as test_global_rpm_cap's."""
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_LLM_SHARED_WINDOW", "0")
    monkeypatch.setenv("AIFORGE_LLM_MAX_RPM", "0")
    monkeypatch.setenv("AIFORGE_COMPACTION_RPM", "0")
    monkeypatch.setenv("AIFORGE_CHAT_RPM", "0")
    from aiforge_core.config import _filecache
    _filecache.clear()
    rl._RPM_BUCKETS.clear()
    rl.reset_global()
    yield
    rl._RPM_BUCKETS.clear()
    rl.reset_global()


def test_an_unthrottled_compaction_send_is_still_counted():
    before = rl.global_used()
    rl.acquire_global(role="learner")
    assert rl.global_used() == before + 1


def test_it_is_counted_through_the_one_gateway_too():
    """`govern_send` is what every real send calls — including the structured
    (OKF/memory) path that books one charge up front when the metered client
    cannot be built."""
    before = rl.global_used()
    rl.govern_send(role="learner", provider="openai_compatible", model="m",
                   meter=False)
    assert rl.global_used() == before + 1


def test_an_unthrottled_compaction_send_still_never_blocks():
    """Visible, not throttled: counting it must not reintroduce a wait."""
    for _ in range(30):
        assert rl.acquire_global(role="learner") == 0.0


def test_chat_keeps_no_window_when_no_ceiling_was_asked_for():
    """The other half of the contract, unchanged — see
    test_global_rpm_cap.test_a_rejection_is_obeyed_even_with_no_ceiling_set."""
    rl.acquire_global()
    rl.acquire_global(role="doer")
    assert rl.global_used() == 0
