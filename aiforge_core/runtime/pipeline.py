"""Native ADK ``Workflow`` graph factory for the v6 pipeline.

ADK 2.x deprecated ``SequentialAgent`` / ``ParallelAgent`` / ``LoopAgent``
in favour of the graph-based :class:`google.adk.workflow.Workflow`:
explicit nodes wired by ``Edge``s, parallel fan-out + ``JoinNode``, and
conditional routing where a node emits ``ctx.route`` and the matching edge
fires. This module wires that graph.

Graph shape::

    START → triage_gate ──trivial──────────────────────────────► doer
                       └──full──► enhancer
        enhancer ─┬► researcher ─┐
                  ├► ctx_memory ─┤
                  ├► ctx_repomap ┤ (parallel)  → context_join → merge_context
                  └► ctx_conv ───┘                                   │
                                                                     ▼
        planner ─┬► verify_correctness ─┐                        planner
                 ├► verify_scope ───────┤ (parallel) → verifier_join
                 └► verify_risk ────────┘        → merge_verdicts → doer
        doer → refiner → feedback → loop_gate ──loop──► doer
                                             └──exit──► validator
        validator → validator_gate ──replan──► planner   (once)
                                   └──done────► learner

Parallel fan-outs replace ``ParallelAgent``; the Doer loop's ``loop_gate``
(iteration counter + LOC-plateau + Feedback verdict) replaces
``LoopAgent``; ``triage_gate`` / ``validator_gate`` express the fast-path
and replan edges. Triage complexity may be pre-seeded in state; absent, the
graph takes the full path. Agents run as ``chat``-mode graph nodes so each
stage still sees prior stages' output.

Each ``LlmAgent`` is wrapped around an :class:`EscalatingLlm` so the
local mlx-lm primary auto-falls-over to the operator's cloud chain
(Ollama Cloud → Anthropic → claude_local) without the orchestrator
having to know about it.

Per-role provider routing comes from
:func:`aiforge_core.config.agent_config.resolve_litellm` + the cloud
chain helper. Prompts live in :mod:`prompts` so this module stays a
straight wiring layer.
"""
from __future__ import annotations

from aiforge_core.agents import (
    doer as _doer_mod,
)
from aiforge_core.agents import (
    enhancer as _enhancer_mod,
)
from aiforge_core.agents import (
    feedback as _feedback_mod,
)
from aiforge_core.agents import (
    learner as _learner_mod,
)
from aiforge_core.agents import (
    live_verifier as _live_verifier_mod,
)
from aiforge_core.agents import (
    planner as _planner_mod,
)
from aiforge_core.agents import (
    refiner as _refiner_mod,
)
from aiforge_core.agents import (
    validator as _validator_mod,
)
from aiforge_core.config import agent_config as _acfg

from .escalating_llm import EscalatingLlm
from .local_probe import maybe_substitute_primary

# Per-ticket override knob populated by ``adk_runner._process_one_ticket``
# before each ``build_pipeline`` call. None means "respect agent_config";
# a string means "force every archetype onto this provider for this run".
# Module-level because the ADK LlmAgent factory has no clean place to
# thread an option down through and the runner is single-shot anyway.
_FORCE_PROVIDER: str | None = None


def set_force_provider(name: str | None) -> None:
    """Pin every archetype to ``name`` (e.g. ``claude_local``) for the
    next pipeline build. Pass ``None`` to clear."""
    global _FORCE_PROVIDER
    _FORCE_PROVIDER = name


def get_force_provider() -> str | None:
    """Read the current pipeline-wide provider override (or ``None``)."""
    return _FORCE_PROVIDER


def _force_claude_local_cfg(role: str) -> dict:
    """Build a resolve_litellm-shaped dict that pins claude_local with
    the provider's default model — used when a ticket has attachments
    that only the subscription CLI can read."""
    from aiforge_core.config.agent_config import PROVIDERS as _PROV
    prov = _PROV["claude_local"]
    return {
        "model_id": prov["default_model"],
        "api_base": "claude:cli",
        "api_key": "",
        "_claude_cli": True,
    }


def _claude_pinned_model(role: str):
    """Build an :class:`EscalatingLlm` always pinned to ``claude_local``.

    Used for the Enhancer + Validator agents — those stages are
    *always* Claude regardless of the operator's profile because
    local models are weak at re-framing tickets / second-opinion
    judging. We don't pass a cloud chain because Claude IS the
    fallback layer for everything else; degrading further makes no
    sense for these two roles.
    """
    primary = _force_claude_local_cfg(role)
    return EscalatingLlm.build(role, primary, [])


def build_litellm_model(role: str):
    """Return an :class:`EscalatingLlm` for the given role.

    Resolution order:

    1. Per-ticket force override (e.g. attachments → claude_local).
       Disables the cloud chain too — attachments only work through
       the subscription CLI, so falling back to a different provider
       would silently drop the file context.
    2. Operator profile via ``agent_config.resolve_litellm``.
    3. Pre-flight local-endpoint probe — if local mlx-lm is dead,
       swap to ``cloud_default_for_local`` (Ollama Cloud
       ``qwen3-coder-next`` by default) so the agent loop doesn't pay
       a failed-primary round-trip on every turn.

    EscalatingLlm wrapping always applies (primary → cloud chain →
    primary_retry); disable the chain with ``AIFORGE_ESCALATE_DISABLE=1``.
    """
    if _FORCE_PROVIDER == "claude_local":
        primary = _force_claude_local_cfg(role)
        return EscalatingLlm.build(role, primary, [])  # no chain — file
                                                       # context is local
    primary = _acfg.resolve_litellm(role)
    primary = maybe_substitute_primary(role, primary)
    chain = _acfg.cloud_escalation_chain(role)
    return EscalatingLlm.build(role, primary, chain)


def build_pipeline(*, skip_researcher: bool = False,
                    project: str | None = None):
    """Construct the SequentialAgent. Returns the root agent ready for
    ``Runner(agent=..., session_service=...)``.

    Each archetype is built by its own module under
    ``aiforge_core.agents.*`` — the call site below is the ONLY place
    that knows the order in which they run. Adding a new role = drop a
    module and slot it into the right list here; the per-archetype
    files stay declarative.

    Args:
      skip_researcher: when True, omit Researcher from the pipeline.
        Caller (typically :mod:`adk_runner`) decides via
        :func:`researcher_routing.should_skip_researcher`. Saves
        5+ LM calls on greenfield tickets where the Researcher would
        find nothing relevant anyway.
      project: target repo name (``ticket.project``). Drives two
        things: which ``live_verifier`` recipe gets baked into the
        prompt and whether the live_verifier stage is pinned to
        ``claude_local`` (TallyConnector needs Windows-side Claude
        for full coverage; other repos default to the operator's
        configured model).
    """
    from google.adk.workflow import START, Edge, Workflow

    from .graph_pipeline import (
        ROUTE_DONE,
        ROUTE_EXIT,
        ROUTE_FULL,
        ROUTE_LOOP,
        ROUTE_REPLAN,
        ROUTE_TRIVIAL,
        make_loop_gate,
        make_triage_gate,
        make_validator_gate,
    )
    from .loop_budget import build_loop_budget_callbacks
    from .parallel_stages import (
        build_context_branches,
        build_verifier_branches,
        make_context_join,
        make_merge_context_node,
        make_merge_verdicts_node,
        make_verifier_join,
    )

    # ── leaf agents ─────────────────────────────────────────────────────
    enhancer = _enhancer_mod.build(_claude_pinned_model)
    planner = _planner_mod.build(build_litellm_model)
    doer = _doer_mod.build(build_litellm_model)
    # C6 scope guard — block edits outside ``scope_allowlist_globs`` at the
    # tool-call boundary. KISS: one before_tool_callback that rejects with a
    # soft error when the Doer drifts outside scope.
    try:
        from .scope_guard import make_scope_guard_callback
        _scope_cb = make_scope_guard_callback()
        if _scope_cb is not None:
            existing = getattr(doer, "before_tool_callback", None)
            if existing is None:
                doer.before_tool_callback = _scope_cb
            elif isinstance(existing, list):
                doer.before_tool_callback = list(existing) + [_scope_cb]
            else:
                doer.before_tool_callback = [existing, _scope_cb]
    except Exception:
        pass  # scope guard never blocks pipeline boot
    refiner = _refiner_mod.build(build_litellm_model)
    feedback = _feedback_mod.build(build_litellm_model)
    learner = _learner_mod.build(build_litellm_model)
    validator = _validator_mod.build(_claude_pinned_model)

    # Parallel branch agents (researcher + 3 context gatherers; 3 verifiers).
    context_branches = build_context_branches(
        build_litellm_model, skip_researcher=skip_researcher)
    verifier_branches = build_verifier_branches(build_litellm_model)

    # As ``Workflow`` graph nodes, LlmAgents default to single_turn
    # (include_contents='none'), which would blind each stage to the prior
    # stages' outputs. ``chat`` mode preserves the conversation history —
    # matching the old SequentialAgent behaviour where every agent sees
    # what came before. (``task`` mode is rejected for static graph nodes.)
    _agent_nodes = [
        enhancer, planner, doer, refiner, feedback, learner, validator,
        *context_branches, *verifier_branches,
    ]
    for _a in _agent_nodes:
        _a.mode = "chat"

    # ── per-agent callbacks (fire via agent.run_async inside the node) ──
    # Persist Learner-emitted facts into Neo4j (Observation_v2 +
    # Decision_v2). Without this, state['facts_json'] dies with the session.
    from .learner_persist import make_learner_after_callback
    _append_after(learner, make_learner_after_callback())

    # LOC-plateau watcher on the Refiner — sees each loop turn AFTER the Doer
    # reported file_diffs. Sets state['loop_budget_kill'] which loop_gate
    # reads to exit the Doer loop early. (The old LoopAgent before-callback
    # abort is now the loop_gate's ``exit`` route.)
    _plateau_before, plateau_after = build_loop_budget_callbacks()
    if plateau_after is not None:
        _append_after(refiner, plateau_after)

    # Failure-memory after-callback on the Validator — writes a failure
    # Observation_v2 when the run didn't land cleanly.
    try:
        from .failure_memory import make_failure_memory_after_callback
        _append_after(validator, make_failure_memory_after_callback())
    except Exception:
        pass  # failure_memory wiring never blocks pipeline boot

    # ── routing + merge nodes ───────────────────────────────────────────
    triage_gate = make_triage_gate()
    context_join = make_context_join()
    merge_context = make_merge_context_node()
    verifier_join = make_verifier_join()
    merge_verdicts = make_merge_verdicts_node()
    loop_gate = make_loop_gate()
    validator_gate = make_validator_gate()

    # ── graph edges ─────────────────────────────────────────────────────
    # NOTE: live_verifier is intentionally NOT in this graph — it runs
    # standalone AFTER the runner opens the PR (its deploy recipe merges +
    # rolls out the PR before testing). See adk_runner._run_live_verifier.
    edges: list = [
        # entry + fast-path switch
        Edge(from_node=START, to_node=triage_gate),
        Edge(from_node=triage_gate, to_node=doer, route=ROUTE_TRIVIAL),
        Edge(from_node=triage_gate, to_node=enhancer, route=ROUTE_FULL),
    ]
    # context fan-out → join → merge → planner
    for br in context_branches:
        edges.append(Edge(from_node=enhancer, to_node=br))
        edges.append(Edge(from_node=br, to_node=context_join))
    edges.append(Edge(from_node=context_join, to_node=merge_context))
    edges.append(Edge(from_node=merge_context, to_node=planner))
    # verifier fan-out → join → merge → doer
    for br in verifier_branches:
        edges.append(Edge(from_node=planner, to_node=br))
        edges.append(Edge(from_node=br, to_node=verifier_join))
    edges.append(Edge(from_node=verifier_join, to_node=merge_verdicts))
    edges.append(Edge(from_node=merge_verdicts, to_node=doer))
    # doer loop: doer → refiner → feedback → loop_gate ⟲
    edges += [
        Edge(from_node=doer, to_node=refiner),
        Edge(from_node=refiner, to_node=feedback),
        Edge(from_node=feedback, to_node=loop_gate),
        Edge(from_node=loop_gate, to_node=doer, route=ROUTE_LOOP),
        Edge(from_node=loop_gate, to_node=validator, route=ROUTE_EXIT),
        # validator → replan back to planner, or done → learner
        Edge(from_node=validator, to_node=validator_gate),
        Edge(from_node=validator_gate, to_node=planner, route=ROUTE_REPLAN),
        Edge(from_node=validator_gate, to_node=learner, route=ROUTE_DONE),
    ]

    return Workflow(name="aiforge_v6_pipeline", edges=edges)


def _append_after(agent, cb) -> None:
    """Append ``cb`` to ``agent.after_agent_callback`` preserving existing
    callback(s)."""
    if cb is None:
        return
    existing = getattr(agent, "after_agent_callback", None)
    merged: list = []
    if existing is not None:
        if isinstance(existing, list):
            merged.extend(existing)
        else:
            merged.append(existing)
    merged.append(cb)
    agent.after_agent_callback = merged


def build_live_verifier_agent(project: str | None = None):
    """Build the standalone live_verifier agent the runner invokes
    AFTER opening the PR. Always pinned to claude_local — see the note
    in :func:`build_pipeline` for why (context size + tool reliability
    + the operator's "Claude must validate" rule)."""
    return _live_verifier_mod.build(_claude_pinned_model, project=project)


__all__ = [
    "build_pipeline", "build_litellm_model", "build_live_verifier_agent",
    "set_force_provider", "get_force_provider",
]
