"""A team run handed a spec the build route already wrote.

In team mode the build route enhances the request, designs files and splits
it; when that yields a single task it falls through to the sequential team
pipeline, whose own Enhancer used to rewrite the SAME request again — a second
LLM round-trip whose spec could differ from the SPEC.md just written. The
route's spec now rides into the run as state, and the graph's Enhancer returns
it as its reply instead of calling its model: downstream nodes see it in
``enhanced_body`` and in history exactly where the rewrite would sit.
"""
from __future__ import annotations

import asyncio

from google.adk.agents import LlmAgent
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.workflow import START, Edge, Workflow, node
from google.genai import types as gtypes

from aiforge_core.runtime import _pipeline_graph as G
from aiforge_core.runtime import chat_pipeline as P


class _CountingLlm(BaseLlm):
    calls: list = []

    async def generate_content_async(self, llm_request, stream=False):
        self.calls.append(llm_request)
        yield LlmResponse(content=gtypes.Content(
            role="model", parts=[gtypes.Part.from_text(text="# model spec")]))


def _run_enhancer(state: dict):
    """START → enhancer (chat mode, as in the pipeline) → probe. Returns the
    model calls, the probe's view of state, and the enhancer's reply texts."""
    llm = _CountingLlm(model="fake")
    llm.calls = []
    enhancer = LlmAgent(name="enhancer", model=llm, instruction="rewrite",
                        output_key="enhanced_body")
    enhancer.mode = "chat"
    G._append_callback(enhancer, "before_agent_callback",
                       G._make_enhancer_preseed())
    seen: dict = {}

    async def _probe(ctx):
        seen["enhanced_body"] = ctx.state.get("enhanced_body")

    wf = Workflow(name="t", edges=[
        Edge(from_node=START, to_node=enhancer),
        Edge(from_node=enhancer, to_node=node(_probe, name="probe"))])
    G._unstall_chat_nodes(wf)

    async def _go():
        svc = InMemorySessionService()
        r = Runner(agent=wf, app_name="t", session_service=svc,
                   auto_create_session=True)
        s = await svc.create_session(app_name="t", user_id="u", state=state)
        msg = gtypes.Content(role="user",
                             parts=[gtypes.Part.from_text(text="build it")])
        texts = []
        async for ev in r.run_async(user_id="u", session_id=s.id,
                                    new_message=msg):
            if ev.author == "enhancer" and ev.content and ev.content.parts:
                texts.append("".join(p.text or "" for p in ev.content.parts))
        return texts
    texts = asyncio.run(_go())
    return llm.calls, seen, texts


def test_a_pre_enhanced_spec_skips_the_enhancers_model_call():
    calls, seen, texts = _run_enhancer({G.PRE_ENHANCED_KEY: "# route spec"})
    assert calls == [], "the Enhancer must not rewrite a request twice"
    assert seen["enhanced_body"] == "# route spec", \
        "downstream nodes must build against the route's spec"
    assert texts == ["# route spec"], "the spec sits in history as its reply"


def test_without_a_spec_the_enhancer_runs_as_before():
    calls, seen, texts = _run_enhancer({})
    assert len(calls) == 1
    assert seen["enhanced_body"] == "# model spec"
    assert texts == ["# model spec"]


def test_a_blank_spec_is_not_a_spec():
    calls, seen, _ = _run_enhancer({G.PRE_ENHANCED_KEY: "   "})
    assert len(calls) == 1 and seen["enhanced_body"] == "# model spec"


def test_the_pipeline_wires_the_preseed_onto_its_enhancer():
    import types as pytypes
    agents = {k: pytypes.SimpleNamespace() for k in
              ("doer", "refiner", "learner", "planner", "enhancer", "validator")}
    G._attach_agent_callbacks(**agents)
    cbs = agents["enhancer"].before_agent_callback
    st = {G.PRE_ENHANCED_KEY: "S"}
    out = [cb(callback_context=pytypes.SimpleNamespace(state=st)) for cb in cbs]
    assert any(o is not None for o in out) and st["enhanced_body"] == "S"


class _NoBundle:
    rules_md = memory_md = preferences_md = ""

    def blocks(self):
        return []


def _capture_team_state(monkeypatch, **kw):
    from aiforge_core.runtime import context_bundle
    seen: dict = {}
    monkeypatch.setattr(context_bundle, "build_bundle",
                        lambda *a, **k: _NoBundle())

    # *_rest: the driver grew an epoch argument; a fake that refused it
    # raised in the driver thread, the sentinel never came and the test hung.
    def _drive_awake(q, sid, cwd, raw, started, prompt, team_state, *_rest):
        seen.update(team_state)
        q.put(P._SENTINEL)
    monkeypatch.setattr(P, "_drive_awake", _drive_awake)
    evs = list(P.stream_chat_pipeline("build it", cwd="/w", session_id=None,
                                      **kw))
    assert evs[-1] == {"type": "done"}
    return seen


def test_stream_chat_pipeline_seeds_the_spec_into_the_run(monkeypatch):
    st = _capture_team_state(monkeypatch, enhanced="# route spec")
    assert st[G.PRE_ENHANCED_KEY] == "# route spec"


def test_no_spec_leaves_the_run_state_alone(monkeypatch):
    st = _capture_team_state(monkeypatch)
    assert G.PRE_ENHANCED_KEY not in st
