"""``pr_reviewer._llm_review`` — the one send that reached litellm directly.

This path does not go through ``llm.client``, so for as long as nobody noticed
it was spending the gateway's allowance without telling the limiter: uncapped
AND invisible, the same hole the structured path had, in something that fires
once per PR. These pin the three things it now owes every other transport —
throttle, attribute, settle — and the fail-open behaviour that must survive all
three going wrong, because a review is worth more than its bookkeeping.
"""
from __future__ import annotations

import sys
import types

import pytest

from aiforge_core.runtime import pr_reviewer


@pytest.fixture(autouse=True)
def _cfg(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.delenv("AIFORGE_LLM_MAX_RPM", raising=False)
    from aiforge_core.config import _filecache
    from aiforge_core.llm import rate_limiter as rl
    _filecache.clear()
    rl.reset_global()
    yield
    rl.reset_global()


def _fake_litellm(monkeypatch, *, reply='{"verdict": "approve"}', boom=None):
    """Stand in for the real litellm, recording the kwargs it was called with."""
    seen: dict = {}

    def _completion(**kw):
        seen.update(kw)
        if boom is not None:
            raise boom
        return {"choices": [{"message": {"content": reply}}]}

    mod = types.ModuleType("litellm")
    mod.completion = _completion
    monkeypatch.setitem(sys.modules, "litellm", mod)
    return seen


# ── it goes through the one gateway ──────────────────────────────────────

def test_the_review_send_is_charged_to_the_ceiling(monkeypatch):
    """Uncapped AND invisible was the defect. One call, one slot."""
    from aiforge_core.llm import rate_limiter as rl

    _fake_litellm(monkeypatch)
    before = rl.global_used()

    pr_reviewer._llm_review("review this")

    assert rl.global_used() == before + 1


def test_it_asks_the_limiter_for_the_chat_bucket(monkeypatch):
    """Not 'learner'. A PR review is foreground work competing with chat, not
    background distillation, so it must not draw on compaction's small slice."""
    seen: dict = {}
    _fake_litellm(monkeypatch)
    from aiforge_core.llm import rate_limiter as rl

    real = rl.govern_send
    monkeypatch.setattr(rl, "govern_send",
                        lambda **kw: seen.update(kw) or real(**kw))

    pr_reviewer._llm_review("review this")

    assert rl._category(seen["role"]) == "chat"
    assert seen["provider"] == "openai_compatible"


def test_a_limiter_fault_never_costs_the_review(monkeypatch):
    """The limiter is bookkeeping; the review is the product."""
    from aiforge_core.llm import rate_limiter as rl

    seen = _fake_litellm(monkeypatch)
    monkeypatch.setattr(rl, "govern_send",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("down")))

    assert pr_reviewer._llm_review("review this") == {"verdict": "approve"}
    assert seen, "the review never went out"


# ── it says who it is ────────────────────────────────────────────────────

def test_the_call_carries_our_user_agent(monkeypatch):
    from aiforge_core.llm import user_agent as ua

    seen = _fake_litellm(monkeypatch)

    pr_reviewer._llm_review("review this")

    assert seen["extra_headers"]["User-Agent"] == ua.user_agent()


# ── it settles what it counted ───────────────────────────────────────────

def test_a_failed_send_is_recorded_as_a_failure(monkeypatch):
    """A send counted at the gateway and never settled reads as a SUCCESS, so
    the review that failed would be the one the operator cannot see."""
    from aiforge_core.llm import call_meter

    call_meter.reset_all()
    _fake_litellm(monkeypatch, boom=RuntimeError("connection reset"))

    assert pr_reviewer._llm_review("review this") == {}

    snap = call_meter.snapshot()
    assert snap.get("failed", 0) >= 1, snap


def test_a_metering_fault_never_costs_the_review(monkeypatch):
    """Failing to RECORD a failure must not become a second failure."""
    from aiforge_core.llm import call_meter

    _fake_litellm(monkeypatch, boom=RuntimeError("connection reset"))
    monkeypatch.setattr(call_meter, "record_failure",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))

    assert pr_reviewer._llm_review("review this") == {}


def test_the_token_is_bound_before_the_try(monkeypatch):
    """The failure handler reads it. An import raising inside the try would
    otherwise make the handler die on an unbound name — turning a recoverable
    transport error into a NameError."""
    seen = _fake_litellm(monkeypatch, boom=RuntimeError("boom"))
    monkeypatch.setattr(
        "aiforge_core.llm.user_agent.user_agent",
        lambda: (_ for _ in ()).throw(RuntimeError("header failed")))

    assert pr_reviewer._llm_review("review this") == {}
    assert not seen, "the send should not have happened"


# ── fail-open, all the way down ──────────────────────────────────────────

def test_no_litellm_is_not_an_error(monkeypatch):
    """A None in sys.modules is how the import system spells "absent": the
    statement raises ImportError rather than binding None, which is exactly the
    shape a box without the extra sees."""
    monkeypatch.setitem(sys.modules, "litellm", None)
    with pytest.raises(ImportError):
        import litellm  # noqa: F401 — proving the arrangement, not using it
    assert pr_reviewer._llm_review("review this") == {}


def test_an_unparseable_reply_is_no_findings_not_a_crash(monkeypatch):
    """Fail-open, never fail-closed: a flaky model must not wedge an
    autonomous run."""
    _fake_litellm(monkeypatch, reply="I have opinions but no JSON")
    assert pr_reviewer._llm_review("review this") == {}
