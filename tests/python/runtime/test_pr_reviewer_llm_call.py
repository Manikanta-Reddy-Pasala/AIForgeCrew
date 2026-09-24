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


def _clear_model_env(monkeypatch):
    """No box env may leak a model/endpoint into what these tests resolve."""
    import os
    for k in list(os.environ):
        if k.startswith("AIFORGE_") and k.endswith(
                ("_MODEL", "_PROVIDER", "_BASE_URL", "_API_KEY")):
            monkeypatch.delenv(k, raising=False)


@pytest.fixture(autouse=True)
def _cfg(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.delenv("AIFORGE_LLM_MAX_RPM", raising=False)
    _clear_model_env(monkeypatch)
    from aiforge_core.config import _filecache, agent_config
    from aiforge_core.llm import rate_limiter as rl
    _filecache.clear()
    rl.reset_global()
    # The operator's configured doer — what an unpinned reviewer runs on.
    agent_config.set_role("doer", "openai_compatible", "cfg-model",
                          base_url="http://box:1234/v1", api_key="k")
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


# ── it runs on the operator's model, never an invented one ───────────────

def test_an_unpinned_review_uses_the_configured_doer_model(monkeypatch):
    """A hard-coded default made LM Studio JIT-load a second 44 GB model on a
    one-box install (112.9 s for one review) and evict the configured one."""
    seen = _fake_litellm(monkeypatch)

    pr_reviewer._llm_review("review this")

    assert seen["model"] == "openai/cfg-model"
    assert seen["api_base"] == "http://box:1234/v1"
    assert seen["api_key"] == "k"


def test_a_default_row_wins_over_the_doer(monkeypatch):
    """"Apply to all" (the ``_default`` row) is the operator's one endpoint."""
    from aiforge_core.config import agent_config
    agent_config.set_role("_default", "openai_compatible", "all-model",
                          base_url="http://all:1234/v1")
    seen = _fake_litellm(monkeypatch)

    pr_reviewer._llm_review("review this")

    assert seen["model"] == "openai/all-model"
    assert seen["api_base"] == "http://all:1234/v1"


def test_the_env_override_still_wins_on_the_configured_endpoint(monkeypatch):
    monkeypatch.setenv("AIFORGE_REVIEWER_MODEL", "openai/pinned")
    seen = _fake_litellm(monkeypatch)

    pr_reviewer._llm_review("review this")

    assert seen["model"] == "openai/pinned"
    assert seen["api_base"] == "http://box:1234/v1"


def test_nothing_configured_skips_the_review(monkeypatch, tmp_path):
    """No model anywhere → no send at all, not a guess at one."""
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "empty"))
    seen = _fake_litellm(monkeypatch)

    assert pr_reviewer._llm_review("review this") == {}
    assert not seen, "a review went out with no configured model"


def test_review_pr_says_why_it_skipped(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "empty"))
    monkeypatch.setattr(pr_reviewer.shutil, "which", lambda _: "/usr/bin/gh")
    monkeypatch.setattr(pr_reviewer, "_gh_pr_diff",
                        lambda *a: pytest.fail("diff fetched for a skip"))

    out = pr_reviewer.review_pr("https://github.com/o/r/pull/1", "t", "b")

    assert out == {"ok": False, "error": "no_reviewer_model"}


def test_a_bare_override_gets_the_provider_prefix(monkeypatch):
    """litellm refuses an unprefixed id — resolve_litellm prefixes every
    configured model, so the override must be prefixed the same way."""
    monkeypatch.setenv("AIFORGE_REVIEWER_MODEL", "vendor/bare-id")
    seen = _fake_litellm(monkeypatch)

    pr_reviewer._llm_review("review this")

    assert seen["model"] == "openai/vendor/bare-id"


# ── same send shape as the pipeline ──────────────────────────────────────

def test_the_send_drops_params_a_strict_endpoint_rejects(monkeypatch):
    seen = _fake_litellm(monkeypatch)
    pr_reviewer._llm_review("review this")
    assert seen["drop_params"] is True


def test_reasoning_off_rides_in_the_body(monkeypatch):
    from aiforge_core.llm import reasoning
    monkeypatch.setenv("AIFORGE_NO_REASONING", "1")
    seen = _fake_litellm(monkeypatch)

    pr_reviewer._llm_review("review this")

    assert seen["extra_body"] == reasoning.NO_THINK_KWARGS


def test_an_insecure_tls_endpoint_skips_verification(monkeypatch):
    """A self-signed internal endpoint the operator marked insecure: the review
    must not die on CERTIFICATE_VERIFY_FAILED where the pipeline succeeds."""
    from aiforge_core.config import agent_config
    monkeypatch.delenv("AIFORGE_LLM_CA_BUNDLE", raising=False)
    agent_config.set_role("doer", "openai_compatible", "cfg-model",
                          base_url="https://box.internal/v1",
                          insecure_tls=True)
    seen = _fake_litellm(monkeypatch)

    pr_reviewer._llm_review("review this")

    assert seen["api_base"] == "https://box.internal/v1"
    assert seen["ssl_verify"] is False


def test_a_secure_endpoint_keeps_verification(monkeypatch):
    seen = _fake_litellm(monkeypatch)
    pr_reviewer._llm_review("review this")
    assert "ssl_verify" not in seen
