"""Mid-run steering (Gap A, team mode) — the Doer/Refiner before_model
callback (chat_steer_callback) must fold a steer pushed via
chat_interject.push() into the very NEXT executor model call, using the
REAL ADK graph (not a mock of the callback) so this proves the mechanism
actually reaches a live invocation — not just that the callback function
does what it's told in isolation.

Both doer AND refiner carry the callback (lowest latency to apply a steer —
whichever runs next picks it up), so with the loop doer1 -> refiner1 ->
feedback(fail) -> doer2 -> refiner2 -> feedback(pass) -> exit, a steer
pushed while doer1 is still in flight is drained by refiner1 (the very
next model call after the push), NOT doer2 — confirmed empirically before
writing this assertion (an earlier version of this test wrongly expected
doer2 and failed; instrumenting the real callback showed refiner1 got to
it first).
"""
from __future__ import annotations

import asyncio

import pytest
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import types as gt

SESSION_ID = 424242


def _contents_text(llm_request) -> list[str]:
    return [
        "".join(getattr(p, "text", "") or "" for p in (c.parts or []))
        for c in (getattr(llm_request, "contents", None) or [])
    ]


def _make_stub(role: str, calls: list, doer_contents: list,
               refiner_contents: list, barrier):
    replies = {
        "triage": '{"complexity": "moderate", "estimated_files": 3, '
                  '"rationale": "x"}',
        "validator": '{"verdict": "approve", "rationale": "ok", '
                     '"scope_ok": true, "tests_present": true, '
                     '"regression_risk": "low"}',
        "planner": 'PLAN: {"subtickets": '
                   '[{"scope_allowlist_globs": ["src/a/**"]}]}',
        "verifier": '{"verdict": "pass", "rationale": "ok"}',
    }

    class _Stub(BaseLlm):
        async def generate_content_async(self, llm_request, stream=False):
            calls.append(role)
            if role == "doer":
                doer_contents.append(_contents_text(llm_request))
                if len(doer_contents) == 1:
                    # Deterministic barrier: pause the FIRST doer turn until
                    # the test has pushed its steer — removes timing
                    # guesswork (real tool calls give real wall-clock gaps
                    # between Doer loop iterations; here we force one).
                    barrier["doer1_started"].set()
                    await barrier["steer_pushed"].wait()
            if role == "refiner":
                refiner_contents.append(_contents_text(llm_request))
            if role == "feedback":
                # Loop exactly once: fail the first pass (forces a 2nd
                # doer/refiner round), pass the second (exits the loop).
                text = "fail\nneeds more" if calls.count("feedback") == 1 \
                    else "pass\nall good"
                yield LlmResponse(content=gt.Content(
                    role="model", parts=[gt.Part(text=text)]))
                return
            text = replies.get(role, f"{role} output")
            yield LlmResponse(content=gt.Content(
                role="model", parts=[gt.Part(text=text)]))

    return _Stub(model="stub")


@pytest.fixture
def _stub_pipeline(monkeypatch):
    monkeypatch.setenv("AIFORGE_ESCALATE_DISABLE", "1")
    monkeypatch.setenv("AIFORGE_OBSERVABILITY_DISABLE", "1")
    monkeypatch.setenv("AIFORGE_CURRENT_SESSION", str(SESSION_ID))
    import aiforge_core.runtime.pipeline as pl
    from aiforge_core.runtime import chat_interject
    chat_interject.clear(SESSION_ID)
    calls: list = []
    doer_contents: list = []
    refiner_contents: list = []
    barrier = {"doer1_started": asyncio.Event(), "steer_pushed": asyncio.Event()}
    monkeypatch.setattr(pl, "build_litellm_model",
                        lambda role: _make_stub(role, calls, doer_contents,
                                                refiner_contents, barrier))
    yield pl, calls, doer_contents, refiner_contents, barrier
    chat_interject.clear(SESSION_ID)


def test_steer_pushed_mid_run_reaches_next_executor_call(_stub_pipeline):
    from aiforge_core.runtime import chat_interject
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService

    pl, calls, doer_contents, refiner_contents, barrier = _stub_pipeline
    wf = pl.build_pipeline(skip_researcher=True)

    async def push_steer_when_ready():
        await barrier["doer1_started"].wait()
        chat_interject.push(SESSION_ID, "focus on error handling")
        barrier["steer_pushed"].set()

    async def _go():
        svc = InMemorySessionService()
        runner = Runner(agent=wf, app_name="t", session_service=svc,
                        auto_create_session=True)
        session = await svc.create_session(app_name="t", user_id="u")
        msg = gt.Content(role="user", parts=[gt.Part(text="# T-1\nfix the thing")])
        pusher = asyncio.create_task(push_steer_when_ready())
        async for _ in runner.run_async(user_id="u", session_id=session.id,
                                        new_message=msg):
            pass
        await pusher

    asyncio.run(asyncio.wait_for(_go(), timeout=120))

    assert calls.count("doer") == 2, f"expected 2 doer iterations, got: {calls}"
    STEER_MARK = "[mid-run instruction — follow this now]: focus on error handling"
    # doer's FIRST call was already in flight (barrier-held) before the push
    # — it must never see it.
    assert STEER_MARK not in " ".join(doer_contents[0])
    # refiner runs immediately after doer1 completes — the very next model
    # call after the push — so it's the one that actually drains it.
    assert STEER_MARK in " ".join(refiner_contents[0])
    # Once drained, it must not double-apply to any later executor call.
    assert STEER_MARK not in " ".join(doer_contents[1])
    assert STEER_MARK not in " ".join(refiner_contents[1])

    # The callback recorded what it applied; nothing left dangling.
    assert chat_interject.pending(SESSION_ID) is False
