"""Native ADK ``Workflow`` graph factory for the v6 pipeline.

ADK 2.x deprecated ``SequentialAgent`` / ``ParallelAgent`` / ``LoopAgent``
in favour of the graph-based :class:`google.adk.workflow.Workflow`:
explicit nodes wired by ``Edge``s, parallel fan-out + ``JoinNode``, and
conditional routing where a node emits ``ctx.route`` and the matching edge
fires. This module wires that graph.

Graph shape::

    START → triage → triage_gate ──trivial─────────────────────► doer
                                └──full──► enhancer
        enhancer ─┬► researcher ──┐
                  ├► ctx_repomap ─┤ (parallel) → context_join → merge_context
                  └► ctx_conv? ───┘  (conv skipped when repo rules exist)
                                                                     │
                                                                     ▼
        planner → verifier (1 call: correctness+scope+risk) → verifier_gate
                 ──pass──► doer    ──replan──► planner
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
(Ollama Cloud) without the orchestrator having to know about it.

Per-role provider routing comes from
:func:`aiforge_core.config.agent_config.resolve_litellm` + the cloud
chain helper. Prompts live in :mod:`prompts` so this module stays a
straight wiring layer.
"""
from __future__ import annotations

import logging
import os

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
    gap_eval as _gap_eval_mod,
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
    triage as _triage_mod,
)
from aiforge_core.agents import (
    validator as _validator_mod,
)
from aiforge_core.agents import (
    verifier as _verifier_mod,
)
from aiforge_core.config import agent_config as _acfg

from .escalating_llm import EscalatingLlm
from .local_probe import maybe_substitute_primary

log = logging.getLogger("aiforge.pipeline")

# Per-ticket override knob populated by ``adk_runner._process_one_ticket``
# before each ``build_pipeline`` call. None means "respect agent_config";
# a string means "force every archetype onto this provider for this run".
# Module-level because the ADK LlmAgent factory has no clean place to
# thread an option down through and the runner is single-shot anyway.
_FORCE_PROVIDER: str | None = None


def set_force_provider(name: str | None) -> None:
    """Pin every archetype to ``name`` (e.g. ``ollama_cloud``) for the
    next pipeline build. Pass ``None`` to clear."""
    global _FORCE_PROVIDER
    _FORCE_PROVIDER = name


def get_force_provider() -> str | None:
    """Read the current pipeline-wide provider override (or ``None``)."""
    return _FORCE_PROVIDER


def _forced_primary_cfg(role: str, provider: str) -> dict | None:
    """resolve_litellm-shaped cfg pinning ``role`` onto ``provider``'s
    default model. Returns ``None`` for an unknown provider so the caller
    falls back to the role's configured model."""
    prov = _acfg.PROVIDERS.get(provider)
    if prov is None:
        return None
    model = prov.get("default_model") or _acfg._local_default_model()
    prefix = prov["litellm_prefix"]
    if not any(model.startswith(p) for p in _acfg.KNOWN_PREFIXES):
        model = f"{prefix}/{model}"
    api_key = os.environ.get(prov["api_key_env"]) or prov["api_key_default"]
    cfg: dict = {
        "model_id": model, "api_base": prov.get("base_url"), "api_key": api_key,
    }
    return cfg


def build_litellm_model(role: str):
    """Return an :class:`EscalatingLlm` for the given role.

    Resolution order:

    1. Operator profile via ``agent_config.resolve_litellm``.
    2. Pre-flight local-endpoint probe — if local mlx-lm is dead,
       swap to ``cloud_default_for_local`` (Ollama Cloud
       ``qwen3-coder-next`` by default) so the agent loop doesn't pay
       a failed-primary round-trip on every turn.

    EscalatingLlm wrapping always applies (primary → cloud chain →
    primary_retry); disable the chain with ``AIFORGE_ESCALATE_DISABLE=1``.

    A per-run :func:`set_force_provider` pin (e.g. a ticket forced onto
    ``ollama_cloud``) overrides the role's configured provider for this
    build.
    """
    if _FORCE_PROVIDER:
        forced = _forced_primary_cfg(role, _FORCE_PROVIDER)
        if forced is not None:
            chain = _acfg.cloud_escalation_chain(role)
            return EscalatingLlm.build(role, forced, chain)
    primary = _acfg.resolve_litellm(role)
    primary = maybe_substitute_primary(role, primary)
    chain = _acfg.cloud_escalation_chain(role)
    return EscalatingLlm.build(role, primary, chain)


def build_pipeline(*, skip_researcher: bool = False,
                    skip_conventions: bool = False,
                    skip_repomap: bool = False,
                    project: str | None = None):
    """Construct the v6 ``Workflow`` graph. Returns the root node ready
    for ``Runner(agent=..., session_service=...)``.

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
        prompt baked into the live_verifier stage.
    """
    from google.adk.workflow import START, Edge, Workflow

    from .graph_pipeline import (
        ROUTE_DONE,
        ROUTE_EXIT,
        ROUTE_FULL,
        ROUTE_LOOP,
        ROUTE_REPLAN,
        ROUTE_RESEARCH_GAP,
        ROUTE_RESEARCH_OK,
        ROUTE_TRIVIAL,
        ROUTE_VERIFY_PASS,
        ROUTE_VERIFY_REPLAN,
        make_gap_gate,
        make_loop_gate,
        make_plan_promote,
        make_triage_gate,
        make_validator_gate,
        make_verifier_gate,
    )
    from .loop_budget import build_loop_budget_callbacks
    from .parallel_stages import (
        build_context_branches,
        make_context_join,
        make_merge_context_node,
        make_research_entry_node,
    )

    # ── leaf agents ─────────────────────────────────────────────────────
    # Triage runs FIRST as a cheap single-turn classifier; its
    # ``triage_verdict`` (complexity) feeds triage_gate's fast-path
    # decision. Without this node nothing populates the verdict and the
    # graph always takes the full path.
    triage = _triage_mod.build(build_litellm_model)
    enhancer = _enhancer_mod.build(build_litellm_model)
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
    # Stuck-loop guard — stop the Doer re-emitting the same (often
    # malformed) tool call until it burns the whole LLM-call budget.
    try:
        from .repeat_guard import make_repeat_guard_callback
        _rep_cb = make_repeat_guard_callback()
        if _rep_cb is not None:
            existing = getattr(doer, "before_tool_callback", None)
            if existing is None:
                doer.before_tool_callback = _rep_cb
            elif isinstance(existing, list):
                doer.before_tool_callback = list(existing) + [_rep_cb]
            else:
                doer.before_tool_callback = [existing, _rep_cb]
    except Exception:
        pass
    # Human-approval gate — honor allow/ask/deny + risk in the pipeline too.
    # Blocks for Approve/Reject ONLY when an interactive chat approver is
    # attached; autonomous ticket runs fall straight through (no hang).
    # Last in the chain so scope/repeat guards short-circuit before we ask.
    try:
        from .tool_gate import make_approval_gate_callback
        _gate_cb = make_approval_gate_callback()
        if _gate_cb is not None:
            existing = getattr(doer, "before_tool_callback", None)
            if existing is None:
                doer.before_tool_callback = _gate_cb
            elif isinstance(existing, list):
                doer.before_tool_callback = list(existing) + [_gate_cb]
            else:
                doer.before_tool_callback = [existing, _gate_cb]
    except Exception:
        pass
    # A1 quality gate signals — record run_tests/typecheck/format results
    # into tests_ok/typecheck_ok/lint_ok so the Feedback agent's
    # deterministic gate (quality_gate.evaluate) actually has inputs.
    try:
        from .quality_gate import make_quality_signal_callback
        _qs_cb = make_quality_signal_callback()
        existing_after_tool = getattr(doer, "after_tool_callback", None)
        if existing_after_tool is None:
            doer.after_tool_callback = _qs_cb
        elif isinstance(existing_after_tool, list):
            doer.after_tool_callback = list(existing_after_tool) + [_qs_cb]
        else:
            doer.after_tool_callback = [existing_after_tool, _qs_cb]
    except Exception:
        pass  # signal wiring never blocks pipeline boot
    refiner = _refiner_mod.build(build_litellm_model)
    feedback = _feedback_mod.build(build_litellm_model)
    learner = _learner_mod.build(build_litellm_model)
    validator = _validator_mod.build(build_litellm_model)
    # Research-gap critic — only meaningful when the Researcher ran.
    gap_eval = _gap_eval_mod.build(build_litellm_model) \
        if not skip_researcher else None

    # Parallel branch agents (researcher + context gatherers; 3 verifiers).
    # skip_conventions: the runner found glob-scoped repo rules files —
    # those ARE the conventions, injected free via {rules_md?}, so the
    # paid ctx_conventions LLM branch is dropped.
    context_branches = build_context_branches(
        build_litellm_model, skip_researcher=skip_researcher,
        skip_conventions=skip_conventions, skip_repomap=skip_repomap)
    # Single multi-axis plan verifier (one LLM call judging correctness +
    # scope + risk) — replaced the 3 parallel verify_* branches. They ran
    # in parallel (no latency win) but cost 3x tokens to judge one plan.
    verifier = _verifier_mod.build(build_litellm_model)

    # As ``Workflow`` graph nodes, LlmAgents default to single_turn
    # (include_contents='none'), which would blind each stage to the prior
    # stages' outputs. ``chat`` mode preserves the conversation history —
    # matching the old SequentialAgent behaviour — for the agents that
    # genuinely need it (multi-turn tool users + judges of the run's
    # history). (``task`` mode is rejected for static graph nodes.)
    _agent_nodes = [
        enhancer, planner, doer, refiner, feedback, learner,
        *context_branches,
    ]
    for _a in _agent_nodes:
        _a.mode = "chat"
    # Tool-less single-shot judges run single_turn: they read everything
    # they need from state-templated prompt blocks ({plan_md?} etc.), so
    # replaying the full 22-node history into each of them wastes tokens
    # massively (3 verifiers × full history × up to 4 planner epochs,
    # plus the validator) and re-creates the ONE-117 KV pressure.
    # single_turn → include_contents='none'.
    _single_turn = [triage, validator, verifier]
    if gap_eval is not None:
        _single_turn.append(gap_eval)
    for _a in _single_turn:
        _a.mode = "single_turn"

    # Parallel branches share a JoinNode: if ONE branch raises (flaky local
    # mlx-lm), the ADK workflow engine sets error_shut_down and the whole
    # graph aborts — the join never fires, planning/doing never runs. Give
    # the fan-out branches a light node-level retry so a transient blip
    # retries instead of nuking the run. (EscalatingLlm already handles
    # model-layer fallover; this guards the exhausted-chain re-raise.)
    try:
        from google.adk.workflow import RetryConfig
        _branch_retry = RetryConfig(max_attempts=2, initial_delay=1.0,
                                    backoff_factor=2.0)
        for _b in (*context_branches, verifier):
            _b.retry_config = _branch_retry
        # The serial chokepoints too: enhancer/validator/planner/doer/triage
        # sit on the critical path — a single transient exception in any
        # of them is error_shut_down for the whole graph.
        _crit = [triage, enhancer, planner, doer, validator]
        if gap_eval is not None:
            _crit.append(gap_eval)
        for _b in _crit:
            _b.retry_config = _branch_retry
    except Exception:
        pass  # retry is best-effort; never block pipeline boot

    # ── per-agent callbacks (fire via agent.run_async inside the node) ──
    # Persist Learner-emitted facts into Neo4j (Observation_v2 +
    # Decision_v2). Without this, state['facts_json'] dies with the session.
    from .learner_persist import make_learner_after_callback
    _append_after(learner, make_learner_after_callback())

    # Auto-consolidation after-callback on the Learner — mines the finished
    # run's trajectory for durable facts (extract → decide ADD/UPDATE/DELETE/
    # NOOP vs existing → reflect), complementing the explicit facts_json
    # path above. Soft-fail; neo4j-only; feature-flagged.
    try:
        from .memory_consolidate import make_consolidate_after_callback
        _append_after(learner, make_consolidate_after_callback())
    except Exception:
        pass  # consolidation wiring never blocks pipeline boot

    # Record the Planner's decomposition as internal subtasks on the ticket
    # (event-sourced) so the UI charts the breakdown + the Doer flips each
    # subtask's status as it works through them.
    from .subtasks_callback import make_planner_subtasks_callback
    _append_after(planner, make_planner_subtasks_callback())

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
    loop_gate = make_loop_gate()
    validator_gate = make_validator_gate()
    verifier_gate = make_verifier_gate()
    plan_promote = make_plan_promote()
    research_entry = make_research_entry_node()
    gap_gate = make_gap_gate() if not skip_researcher else None

    # ── graph edges ─────────────────────────────────────────────────────
    # NOTE: live_verifier is intentionally NOT in this graph — it runs
    # standalone AFTER the runner opens the PR (its deploy recipe merges +
    # rolls out the PR before testing). See adk_runner._run_live_verifier.
    edges: list = [
        # entry + fast-path switch
        Edge(from_node=START, to_node=triage),
        Edge(from_node=triage, to_node=triage_gate),
        Edge(from_node=triage_gate, to_node=doer, route=ROUTE_TRIVIAL),
        Edge(from_node=triage_gate, to_node=enhancer, route=ROUTE_FULL),
    ]
    # context fan-out: enhancer → research_entry → branches → join → merge.
    # research_entry is the stable fan-out source so the research-gap loop
    # can re-enter it and re-fire ALL branches in one scheduler wave
    # (JoinNode re-arm requirement).
    edges.append(Edge(from_node=enhancer, to_node=research_entry))
    if not context_branches:
        # Lean: no context gatherers at all → go straight to the Planner.
        # (research_entry is a no-op fan-out source; it just passes through.)
        edges.append(Edge(from_node=research_entry, to_node=planner))
    else:
        for br in context_branches:
            edges.append(Edge(from_node=research_entry, to_node=br))
            edges.append(Edge(from_node=br, to_node=context_join))
        edges.append(Edge(from_node=context_join, to_node=merge_context))
        if gap_gate is not None:
            # merge_context → gap_eval → gap_gate ─┬ research_ok  → planner
            #                                       └ research_gap → research_entry
            edges.append(Edge(from_node=merge_context, to_node=gap_eval))
            edges.append(Edge(from_node=gap_eval, to_node=gap_gate))
            edges.append(Edge(from_node=gap_gate, to_node=planner,
                              route=ROUTE_RESEARCH_OK))
            edges.append(Edge(from_node=gap_gate, to_node=research_entry,
                              route=ROUTE_RESEARCH_GAP))
        else:
            edges.append(Edge(from_node=merge_context, to_node=planner))
    # planner → plan_promote (parse plan JSON → scope_allowlist_globs in
    # state) → verifier fan-out → join → merge → verifier_gate
    edges.append(Edge(from_node=planner, to_node=plan_promote))
    # plan_promote → single verifier (judges correctness+scope+risk in one
    # call, writes verifier_verdict) → verifier_gate.
    edges.append(Edge(from_node=plan_promote, to_node=verifier))
    # verifier_gate ACTS on the verdict: a rejected plan loops back to the
    # planner once (bounded); a passing plan proceeds to the Doer.
    edges.append(Edge(from_node=verifier, to_node=verifier_gate))
    edges.append(Edge(from_node=verifier_gate, to_node=doer,
                      route=ROUTE_VERIFY_PASS))
    edges.append(Edge(from_node=verifier_gate, to_node=planner,
                      route=ROUTE_VERIFY_REPLAN))
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

    # Cap concurrent graph-scheduled nodes. The 4-way context fan-out
    # against a single local mlx-lm endpoint is queueing, not parallelism
    # — the server processes serially while 4 in-flight chat-mode prompts
    # multiply KV-cache pressure (the ONE-117 OOM recipe). Floor is 3:
    # with a smaller cap a replan pass can re-fire the 3 verify branches
    # across two scheduler waves, and ADK's JoinNode then sees the
    # not-yet-rescheduled third branch's stale pass-1 COMPLETED status
    # and fires early with the old axis verdict (double-running
    # merge_verdicts + verifier_gate). Raise for cloud providers via
    # AIFORGE_WORKFLOW_MAX_CONCURRENCY (0 = unlimited).
    _cap = int(os.environ.get("AIFORGE_WORKFLOW_MAX_CONCURRENCY", "3"))
    if 0 < _cap < 3:
        _cap = 3
    wf = Workflow(
        name="aiforge_v6_pipeline",
        edges=edges,
        max_concurrency=_cap if _cap > 0 else None,
    )
    # CRITICAL un-stall: ADK's graph builder CLONES every LlmAgent into
    # the graph and forces wait_for_output=True for mode="chat"
    # (conversational re-trigger semantics, _workflow_graph_utils.py).
    # A chat node here never yields an engine "output" — its reply is
    # message_as_output content — so the node parks in WAITING and
    # downstream never triggers: the run stalled right after the
    # enhancer. Our chat agents are one-shot graph stages; flip the
    # flag on the CLONES (mutating the pre-construction originals is
    # useless — the clone step overwrites it).
    from google.adk.agents import LlmAgent as _LlmAgent
    for _n in wf.graph.nodes:
        if isinstance(_n, _LlmAgent) and getattr(_n, "mode", None) == "chat":
            _n.wait_for_output = False
    return wf


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
    AFTER opening the PR. Runs on the operator's configured model (with
    the cloud escalation chain) like every other archetype."""
    return _live_verifier_mod.build(build_litellm_model, project=project)


__all__ = [
    "build_pipeline", "build_litellm_model", "build_live_verifier_agent",
    "set_force_provider", "get_force_provider",
]
