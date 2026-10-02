"""After the one allowed replan the verifier gate proceeds whatever the second
verdict says, so the second verifier model call is skipped."""
from types import SimpleNamespace

from aiforge_core.agents import verifier


def _ctx(**state):
    return SimpleNamespace(state=dict(state))


def test_first_verification_still_runs():
    assert verifier._reverify_skip(callback_context=_ctx()) is None
    assert verifier._reverify_skip(
        callback_context=_ctx(verify_replan_count=0)) is None


def test_second_verification_is_skipped_and_says_why():
    ctx = _ctx(verify_replan_count=1)
    out = verifier._reverify_skip(callback_context=ctx)
    assert out is not None
    assert ctx.state["verifier_verdict"]["skipped"] is True
    from aiforge_core.runtime.graph_pipeline._parsers import _parse_verdict
    assert _parse_verdict(ctx.state["verifier_verdict"]) == "pass"


def test_the_stage_callback_still_runs_when_not_skipping():
    calls = []
    cb = verifier._chain(verifier._reverify_skip,
                         lambda **kw: calls.append(1))
    cb(callback_context=_ctx())
    assert calls == [1]
    cb(callback_context=_ctx(verify_replan_count=1))
    assert calls == [1]
