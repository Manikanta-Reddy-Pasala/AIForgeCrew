"""Team-mode chat pipeline must never surface the Enhancer's raw
``ENHANCE_BLOCKED: <reason>`` sentinel (its stand-in for a clarifying
question — the Enhancer prompt forbids asking one) as a thought bubble, and
must stop instead of burning a full Planner/Doer run on a brief it already
flagged as too vague. Mirrors the ticket-path fix in
adk_runner._enhancer_block_reason, using the real ADK graph + stub LLMs
(same harness as test_pipeline_e2e_stub.py) so this is an actual run, not a
mock of the detection logic.
"""
from __future__ import annotations

import pytest
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import types as gt


def _make_stub(role: str, calls: list, replies: dict):
    class _Stub(BaseLlm):
        async def generate_content_async(self, llm_request, stream=False):
            calls.append(role)
            text = replies.get(role, f"{role} output")
            yield LlmResponse(content=gt.Content(
                role="model", parts=[gt.Part(text=text)]))

    return _Stub(model="stub")


@pytest.fixture
def _stub_pipeline(monkeypatch):
    monkeypatch.setenv("AIFORGE_ESCALATE_DISABLE", "1")
    monkeypatch.setenv("AIFORGE_OBSERVABILITY_DISABLE", "1")
    monkeypatch.setenv("AIFORGE_CHAT_LEAN", "1")   # skip researcher/ctx agents
    import aiforge_core.runtime.pipeline as pl
    calls: list = []
    replies = {
        "triage": '{"complexity": "moderate", "estimated_files": 3, '
                  '"rationale": "x"}',
        "enhancer": "ENHANCE_BLOCKED: no goal extractable from body",
        # planner/doer should NEVER be reached — no reply configured for
        # them means the test fails loudly (KeyError-shaped output) if the
        # graph mistakenly proceeds past the block.
    }
    monkeypatch.setattr(pl, "build_litellm_model",
                        lambda role: _make_stub(role, calls, replies))
    return calls


def test_enhance_blocked_stops_run_and_hides_sentinel(_stub_pipeline, tmp_path):
    from aiforge_core.runtime import chat_pipeline as cp

    calls = _stub_pipeline
    events = list(cp.stream_chat_pipeline(
        "vague nonsense", cwd=str(tmp_path), session_id=None, history=[]))

    # The raw sentinel never reaches the UI as a thought bubble.
    assert not any(
        e.get("type") == "thought" and e.get("role") == "enhancer"
        and "ENHANCE_BLOCKED" in (e.get("text") or "")
        for e in events)

    # Run stopped right after the enhancer — planner/doer never fired.
    assert "enhancer" in calls
    assert "planner" not in calls
    assert "doer" not in calls

    # Final message is the friendly rewrite, not the raw sentinel.
    msgs = [e for e in events if e.get("type") == "message"]
    assert msgs, "no final message emitted"
    final = msgs[-1]["text"]
    assert "ENHANCE_BLOCKED" not in final
    assert "no goal extractable from body" in final
