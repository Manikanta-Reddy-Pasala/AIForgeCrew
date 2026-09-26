"""Every mode's entry point waits out a model outage and carries on, instead
of aborting the work (llm/model_wait). Stub completes: down N times, then up."""
from __future__ import annotations

import asyncio

import pytest

from aiforge_core.llm import endpoint_breaker, model_wait


@pytest.fixture(autouse=True)
def _unbounded_fast(monkeypatch):
    monkeypatch.setenv("AIFORGE_LLM_WAIT_MAX_S", "0")
    monkeypatch.delenv("AIFORGE_CHAT_OUTAGE_WAIT_S", raising=False)

    def _fast(cap=None):
        while True:
            yield 0.01
    monkeypatch.setattr(model_wait, "delays", _fast)
    monkeypatch.setattr(model_wait, "probe", lambda *a, **k: True)
    model_wait._reset_for_tests()
    endpoint_breaker.reset()


def _down_then(n, answer, exc=lambda: ConnectionRefusedError("refused")):
    calls = []

    def _fn(*a, **k):
        calls.append(1)
        if len(calls) <= n:
            raise exc()
        return answer
    return _fn, calls


# ── the client: every direct caller (enhancer, classifier, recall, compaction,
#    learner, planner, structured output, chat's native path) goes through it

def test_client_complete_resends_after_the_outage(monkeypatch):
    from aiforge_core.llm import client
    fn, calls = _down_then(4, "answer")
    monkeypatch.setattr(client, "_complete_impl", fn)
    assert client.complete("learner", [{"role": "user", "content": "x"}]) == "answer"
    assert len(calls) == 5


def test_client_complete_raw_resends_after_the_outage(monkeypatch):
    from aiforge_core.llm import client
    fn, calls = _down_then(3, {"role": "assistant", "content": "ok"},
                           exc=lambda: ConnectionError(
                               "LLM endpoint unreachable (h:1): breaker open"))
    monkeypatch.setattr(client, "_complete_raw_once", fn)
    assert client.complete_raw("chat", [])["content"] == "ok"
    assert len(calls) == 4


def test_client_config_error_still_fails_fast(monkeypatch):
    import io
    import urllib.error
    from aiforge_core.llm import client
    fn, calls = _down_then(9, "never", exc=lambda: urllib.error.HTTPError(
        "http://m/v1", 401, "Unauthorized", {}, io.BytesIO(b"{}")))
    monkeypatch.setattr(client, "_complete_impl", fn)
    with pytest.raises(urllib.error.HTTPError):
        client.complete("chat", [])
    assert len(calls) == 1


# ── chat: simple/act, plan, analyze (and jobs, which run the chat agent) ────

@pytest.mark.parametrize("mode", ["act", "plan", "analyze"])
def test_chat_modes_wait_and_finish(tmp_path, mode):
    from aiforge_core.runtime import chat_agent as ca
    fn, calls = _down_then(3, "FINAL: all done")
    evs = list(ca.run_chat_agent([{"role": "user", "content": "hi"}],
                                 cwd=str(tmp_path), complete_fn=fn, mode=mode))
    assert any(e.get("type") == "message" and "all done" in e.get("text", "")
               for e in evs)
    assert not any(e.get("type") == "stopped" for e in evs)
    assert any("waiting for the model" in e.get("text", "") for e in evs)


def test_chat_client_wait_status_becomes_a_thought(monkeypatch):
    """A wait INSIDE the client call (the default complete path) shows as a
    thought line in the chat stream."""
    from aiforge_core.runtime import chat_cancel
    from aiforge_core.runtime.chat_agent._context import _generation as g

    def complete_fn(role, convo):
        return model_wait.call_with_wait(_down_then(2, "FINAL: ok")[0],
                                         url="http://m/v1")
    chat_cancel.start(4242)
    try:
        gen = g._complete_live(complete_fn, "chat", [], 4242, stream=False)
        evs = []
        try:
            while True:
                evs.append(next(gen))
        except StopIteration as stop:
            out = stop.value
    finally:
        chat_cancel.finish(4242)
    assert out == "FINAL: ok"
    texts = [e.get("text", "") for e in evs if e.get("type") == "thought"]
    assert any("waiting for model at http://m/v1" in t for t in texts)
    assert any("is back" in t for t in texts)


# ── ticket pipeline Doer / parallel subtasks / text doer ───────────────────

def test_parallel_subtask_doer_waits(tmp_path):
    from aiforge_core.runtime.parallel_subtasks._runners import _drive_doer
    fn, calls = _down_then(3, "FINAL: done")
    out = _drive_doer("make it", str(tmp_path), None, fn)
    assert out.get("ok") is True, out


def test_text_doer_waits(tmp_path):
    from aiforge_core.runtime.text_doer import run_text_doer
    fn, calls = _down_then(3, "FINAL: done")
    res = run_text_doer({"ticket_title": "t", "ticket_body": "b"}, str(tmp_path),
                        complete_fn=fn)
    assert len(calls) >= 4
    assert "error" not in str(res.get("doer_outcome", "")).lower()[:40], res


# ── the ADK wrapper: team mode and the ticket pipeline ─────────────────────

def _adk():
    from google.adk.models.base_llm import BaseLlm
    from google.adk.models.llm_response import LlmResponse
    from google.genai import types as gtypes

    class _Down(BaseLlm):
        fails: int = 0
        calls: int = 0

        async def generate_content_async(self, llm_request, stream=False):
            self.calls += 1
            if self.calls <= self.fails:
                raise ConnectionError("Connection refused")
            yield LlmResponse(content=gtypes.Content(
                role="model", parts=[gtypes.Part.from_text(text="up")]))

        @classmethod
        def supported_models(cls):
            return []
    return _Down


@pytest.mark.parametrize("stream", [False, True])
def test_escalating_llm_waits_then_answers(stream):
    from google.adk.models.llm_request import LlmRequest
    from aiforge_core.runtime.escalating_llm import EscalatingLlm
    primary = _adk()(model="primary", fails=5)
    llm = EscalatingLlm(model="primary", role="doer", primary_model=primary,
                        chain_models=[], chain_labels=[])

    async def _go():
        return [r async for r in llm.generate_content_async(
            LlmRequest(model="primary"), stream=stream)]
    out = asyncio.run(_go())
    assert out and out[-1].content.parts[0].text == "up"
    assert primary.calls == 6


def test_escalating_llm_non_outage_still_fails():
    from google.adk.models.llm_request import LlmRequest
    from aiforge_core.runtime.escalating_llm import EscalatingLlm

    class _Bad(_adk()):
        async def generate_content_async(self, llm_request, stream=False):
            self.calls += 1
            raise ValueError("schema mismatch")
            yield  # pragma: no cover
    primary = _Bad(model="primary")
    llm = EscalatingLlm(model="primary", role="doer", primary_model=primary,
                        chain_models=[], chain_labels=[])

    async def _go():
        return [r async for r in llm.generate_content_async(
            LlmRequest(model="primary"), stream=False)]
    with pytest.raises(ValueError):
        asyncio.run(_go())


def test_escalating_llm_wait_is_cancelled_with_the_task():
    from google.adk.models.llm_request import LlmRequest
    from aiforge_core.runtime.escalating_llm import EscalatingLlm
    import threading
    ev = threading.Event()
    primary = _adk()(model="primary", fails=10 ** 9)
    llm = EscalatingLlm(model="primary", role="doer", primary_model=primary,
                        chain_models=[], chain_labels=[])

    async def _go():
        with model_wait.scope(ev, "ticket cancelled"):
            asyncio.get_running_loop().call_later(0.3, ev.set)
            return [r async for r in llm.generate_content_async(
                LlmRequest(model="primary"), stream=False)]
    with pytest.raises(model_wait.ModelWaitCancelled):
        asyncio.run(_go())
    assert primary.calls > 2


# ── pool threads carry the caller's Stop (analysis fan-out, subtasks) ──────

def test_scoped_worker_sees_the_cancel():
    import threading
    ev = threading.Event()
    ev.set()
    run = model_wait.scoped(lambda: model_wait.cancel_reason(), ev, "stopped")
    t_out: list = []
    th = threading.Thread(target=lambda: t_out.append(run()))
    th.start()
    th.join(2)
    assert t_out == ["stopped"]


# ── optional side calls skip instead of waiting ────────────────────────────

def test_title_does_not_wait(monkeypatch):
    from aiforge_core.llm import client
    from aiforge_core.runtime import chat_title
    probed = []
    monkeypatch.setattr(model_wait, "probe", lambda *a, **k: probed.append(1) or True)
    fn, calls = _down_then(1, "Waited Title")
    monkeypatch.setattr(client, "_complete_impl", fn)
    out = chat_title.suggest_title("please fix the login page bug")
    assert out != "Waited Title" and not probed and len(calls) == 1


def test_next_step_prediction_does_not_wait(monkeypatch):
    from aiforge_core.llm import client
    from aiforge_core.runtime.next_step import _predict
    fn, calls = _down_then(1, "x")
    monkeypatch.setattr(client, "_complete_impl", fn)
    with pytest.raises(ConnectionRefusedError):
        _predict._llm("chat", [{"role": "user", "content": "x"}])
    assert len(calls) == 1
