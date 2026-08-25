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


def _build_doer():
    """The Doer node, with every tool-boundary guard attached.

    Doer backend selection: on a LOCAL endpoint the native function-calling
    Doer does nothing (mlx_lm 0.31 "zero tool_use" bug), so fall back to the
    chat agent's proven TEXT protocol wrapped as a FunctionNode. Default
    ``auto`` = text only when the Doer endpoint is local; cloud stays native
    (no behavior change). Soft-fail to native if the switch/import errors.

    A text-doer FunctionNode handles tools INTERNALLY (via run_chat_agent's own
    tool_policy) and replicates the quality signals itself, so these callbacks
    simply don't apply to it — each attach is guarded, so they no-op cleanly.
    """
    try:
        from .text_doer import should_use_text_protocol
        use_text = should_use_text_protocol()
    except Exception:  # noqa: BLE001
        use_text = False
    if use_text:
        from .text_doer import make_text_doer_node
        doer = make_text_doer_node()
    else:
        doer = _doer_mod.build(build_litellm_model)
    for attr, factory in _DOER_TOOL_CALLBACKS:
        try:
            _append_callback(doer, attr, factory())
        except Exception:  # noqa: BLE001 — a guard never blocks pipeline boot
            pass
    return doer


def _scope_guard_cb():
    """C6 scope guard — block edits outside ``scope_allowlist_globs`` at the
    tool-call boundary. KISS: one before_tool_callback that rejects with a soft
    error when the Doer drifts outside scope."""
    from .scope_guard import make_scope_guard_callback
    return make_scope_guard_callback()


def _repeat_guard_cb():
    """Stuck-loop guard — stop the Doer re-emitting the same (often malformed)
    tool call until it burns the whole LLM-call budget."""
    from .repeat_guard import make_repeat_guard_callback
    return make_repeat_guard_callback()


def _approval_gate_cb():
    """Human-approval gate — honor allow/ask/deny + risk in the pipeline too.
    Blocks for Approve/Reject ONLY when an interactive chat approver is
    attached; autonomous ticket runs fall straight through (no hang). Last of
    the before_tool guards so scope/repeat short-circuit before we ask."""
    from .tool_gate import make_approval_gate_callback
    return make_approval_gate_callback()


def _quality_signal_cb():
    """A1 quality gate signals — record run_tests/typecheck/format results into
    tests_ok/typecheck_ok/lint_ok so the Feedback agent's deterministic gate
    (quality_gate.evaluate) actually has inputs."""
    from .quality_gate import make_quality_signal_callback
    return make_quality_signal_callback()


def _hook_before_cb():
    from .hooks import adk_before_tool_callback
    return adk_before_tool_callback()


def _hook_after_cb():
    from .hooks import adk_after_tool_callback
    return adk_after_tool_callback()


# (attribute, factory) in ATTACH ORDER — the guards run in this order, and the
# lifecycle hooks (Claude-Code parity) come last so an operator's hooks.json
# applies to autonomous ticket runs after the built-in guards have had their
# say. AIFORGE_HOOKS_DISABLE=1 makes the hook adapters no-op.
_DOER_TOOL_CALLBACKS = (
    ("before_tool_callback", _scope_guard_cb),
    ("before_tool_callback", _repeat_guard_cb),
    ("before_tool_callback", _approval_gate_cb),
    ("after_tool_callback", _quality_signal_cb),
    ("before_tool_callback", _hook_before_cb),
    ("after_tool_callback", _hook_after_cb),
)


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
        make_gap_gate,
        make_loop_gate,
        make_plan_promote,
        make_triage_gate,
        make_validator_gate,
        make_verifier_gate,
    )
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
    doer = _build_doer()
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

    chat_nodes = [enhancer, planner, doer, refiner, feedback, learner,
                  *context_branches]
    single_turn = [triage, validator, verifier]
    critical = [triage, enhancer, planner, doer, validator]
    if gap_eval is not None:
        single_turn.append(gap_eval)
        critical.append(gap_eval)
    _set_node_modes(chat_nodes, single_turn)
    _set_node_retries((*context_branches, verifier), critical)
    _attach_agent_callbacks(doer=doer, refiner=refiner, learner=learner,
                            planner=planner, enhancer=enhancer,
                            validator=validator)

    # ── routing + merge nodes ───────────────────────────────────────────
    nodes = {
        "triage": triage, "enhancer": enhancer, "planner": planner,
        "doer": doer, "refiner": refiner, "feedback": feedback,
        "learner": learner, "validator": validator, "verifier": verifier,
        "gap_eval": gap_eval, "context_branches": context_branches,
        "triage_gate": make_triage_gate(),
        "context_join": make_context_join(),
        "merge_context": make_merge_context_node(),
        "loop_gate": make_loop_gate(),
        "validator_gate": make_validator_gate(),
        "verifier_gate": make_verifier_gate(),
        "plan_promote": make_plan_promote(),
        "research_entry": make_research_entry_node(),
        "gap_gate": make_gap_gate() if not skip_researcher else None,
    }

    # ── graph edges ─────────────────────────────────────────────────────
    # NOTE: live_verifier is intentionally NOT in this graph — it runs
    # standalone AFTER the runner opens the PR (its deploy recipe merges +
    # rolls out the PR before testing). See adk_runner._run_live_verifier.
    edges = (_entry_edges(Edge, START, nodes)
             + _context_edges(Edge, nodes)
             + _plan_edges(Edge, nodes)
             + _loop_edges(Edge, nodes))
    wf = Workflow(name="aiforge_v6_pipeline", edges=edges,
                  max_concurrency=_workflow_concurrency())
    _unstall_chat_nodes(wf)
    return wf


def _set_node_modes(chat_nodes, single_turn) -> None:
    """As ``Workflow`` graph nodes, LlmAgents default to single_turn
    (include_contents='none'), which would blind each stage to the prior
    stages' outputs. ``chat`` mode preserves the conversation history —
    matching the old SequentialAgent behaviour — for the agents that genuinely
    need it (multi-turn tool users + judges of the run's history).

    Tool-less single-shot judges stay single_turn: they read everything they
    need from state-templated prompt blocks ({plan_md?} etc.), so replaying the
    full 22-node history into each of them wastes tokens massively (3 verifiers
    × full history × up to 4 planner epochs, plus the validator) and re-creates
    the ONE-117 KV pressure.
    """
    for a in chat_nodes:
        # A text-doer FunctionNode is a pydantic model with no ``mode`` field
        # (it drives its own ReAct loop, so chat/single_turn is meaningless) —
        # setting it raises ValueError. Guard so the node can't break the build.
        try:
            a.mode = "chat"
        except Exception:  # noqa: BLE001
            pass
    for a in single_turn:
        a.mode = "single_turn"


def _set_node_retries(branches, critical) -> None:
    """Parallel branches share a JoinNode: if ONE branch raises (flaky local
    mlx-lm), the ADK workflow engine sets error_shut_down and the whole graph
    aborts — the join never fires, planning/doing never runs. Give the fan-out
    branches a light node-level retry so a transient blip retries instead of
    nuking the run. (EscalatingLlm already handles model-layer fallover; this
    guards the exhausted-chain re-raise.) The serial chokepoints get it too —
    they sit on the critical path, where one transient exception is
    error_shut_down for the whole graph."""
    try:
        from google.adk.workflow import RetryConfig
        retry = RetryConfig(max_attempts=2, initial_delay=1.0,
                            backoff_factor=2.0)
        for b in (*branches, *critical):
            b.retry_config = retry
    except Exception:  # noqa: BLE001 — retry is best-effort
        pass


def _attach_agent_callbacks(*, doer, refiner, learner, planner, enhancer,
                            validator) -> None:
    """Per-agent callbacks (they fire via agent.run_async inside the node)."""
    from .learner_persist import make_learner_after_callback
    from .loop_budget import build_loop_budget_callbacks
    from .subtasks_callback import make_planner_subtasks_callback

    # Persist Learner-emitted facts into the embedded SQLite memory store.
    # Without this, state['facts_json'] dies with the session.
    _append_after(learner, make_learner_after_callback())
    # Record the Planner's decomposition as internal subtasks on the ticket
    # (event-sourced) so the UI charts the breakdown + the Doer flips each
    # subtask's status as it works through them.
    _append_after(planner, make_planner_subtasks_callback())
    # ENHANCER DEGENERATE-OUTPUT GUARD (same gate the parallel +
    # escalated-simple paths get in parallel_subtasks._enhance): the enhancer
    # is a single point of failure; a collapsed rewrite or one that dropped
    # every named anchor must never replace the operator's ask.
    _append_after(enhancer, _make_enhancer_guard())
    # LOC-plateau watcher on the Refiner — sees each loop turn AFTER the Doer
    # reported file_diffs. Sets state['loop_budget_kill'] which loop_gate reads
    # to exit the Doer loop early.
    _, plateau_after = build_loop_budget_callbacks()
    _append_after(refiner, plateau_after)

    # Executor context cleansing — the Doer/Refiner run in chat mode, which
    # replays the planner/enhancer/researcher prologue into them every turn.
    # That hand-off already reaches them via their templated prompt blocks, so
    # strip the redundant prologue and keep only the seed + their own recent
    # loop work. Big win for slow 120B models.
    #
    # Mid-run steering (Gap A, team mode) is registered AFTER focus-trim so an
    # applied steer (the newest content) can never itself be cut by the trim.
    # Doer + Refiner are the iterative nodes a team run spends most of its
    # wall-clock in; see chat_steer_callback.py for why before_model (not a
    # session state write) is the mechanism.
    #
    # Each attach is isolated: a text-doer FunctionNode rejects before_model,
    # and that must not also skip the refiner's callbacks.
    for agent, role in ((doer, "doer"), (refiner, "refiner")):
        for module, factory in (
                (".executor_focus", "make_executor_focus_callback"),
                (".chat_steer_callback", "make_steer_before_model_callback")):
            try:
                mod = __import__(f"aiforge_core.runtime{module}",
                                 fromlist=[factory])
                _append_before_model(agent, getattr(mod, factory)(role))
            except Exception:  # noqa: BLE001 — never block pipeline boot
                pass

    # Auto-consolidation after-callback on the Learner — the graph-backed
    # consolidation store was removed, so this callback is a soft no-op now;
    # kept wired for when a consolidation backend returns. Soft-fail.
    try:
        from .memory_consolidate import make_consolidate_after_callback
        _append_after(learner, make_consolidate_after_callback())
    except Exception:  # noqa: BLE001
        pass
    # Failure-memory after-callback on the Validator — writes a failure
    # Observation_v2 when the run didn't land cleanly.
    try:
        from .failure_memory import make_failure_memory_after_callback
        _append_after(validator, make_failure_memory_after_callback())
    except Exception:  # noqa: BLE001
        pass


def _entry_edges(edge_cls, start, n) -> list:
    """Entry + the cheap fast-path switch."""
    from .graph_pipeline import ROUTE_FULL, ROUTE_TRIVIAL
    return [edge_cls(from_node=start, to_node=n["triage"]),
            edge_cls(from_node=n["triage"], to_node=n["triage_gate"]),
            edge_cls(from_node=n["triage_gate"], to_node=n["doer"],
                     route=ROUTE_TRIVIAL),
            edge_cls(from_node=n["triage_gate"], to_node=n["enhancer"],
                     route=ROUTE_FULL)]


def _context_edges(edge_cls, n) -> list:
    """context fan-out: enhancer → research_entry → branches → join → merge.

    research_entry is the stable fan-out source so the research-gap loop can
    re-enter it and re-fire ALL branches in one scheduler wave (a JoinNode
    re-arm requirement).
    """
    from .graph_pipeline import ROUTE_RESEARCH_GAP, ROUTE_RESEARCH_OK
    edges = [edge_cls(from_node=n["enhancer"], to_node=n["research_entry"])]
    if not n["context_branches"]:
        # Lean: no context gatherers at all → go straight to the Planner.
        # (research_entry is a no-op fan-out source; it just passes through.)
        return edges + [edge_cls(from_node=n["research_entry"], to_node=n["planner"])]
    for br in n["context_branches"]:
        edges.append(edge_cls(from_node=n["research_entry"], to_node=br))
        edges.append(edge_cls(from_node=br, to_node=n["context_join"]))
    edges.append(edge_cls(from_node=n["context_join"], to_node=n["merge_context"]))
    if n["gap_gate"] is None:
        edges.append(edge_cls(from_node=n["merge_context"], to_node=n["planner"]))
        return edges
    # merge_context → gap_eval → gap_gate ─┬ research_ok  → planner
    #                                       └ research_gap → research_entry
    edges += [
        edge_cls(from_node=n["merge_context"], to_node=n["gap_eval"]),
        edge_cls(from_node=n["gap_eval"], to_node=n["gap_gate"]),
        edge_cls(from_node=n["gap_gate"], to_node=n["planner"],
                 route=ROUTE_RESEARCH_OK),
        edge_cls(from_node=n["gap_gate"], to_node=n["research_entry"],
                 route=ROUTE_RESEARCH_GAP),
    ]
    return edges


def _plan_edges(edge_cls, n) -> list:
    """planner → plan_promote (parse plan JSON → scope_allowlist_globs in
    state) → the single verifier (correctness+scope+risk in one call, writes
    verifier_verdict) → verifier_gate, which ACTS on the verdict: a rejected
    plan loops back to the planner once (bounded), a passing plan proceeds."""
    from .graph_pipeline import ROUTE_VERIFY_PASS, ROUTE_VERIFY_REPLAN
    return [
        edge_cls(from_node=n["planner"], to_node=n["plan_promote"]),
        edge_cls(from_node=n["plan_promote"], to_node=n["verifier"]),
        edge_cls(from_node=n["verifier"], to_node=n["verifier_gate"]),
        edge_cls(from_node=n["verifier_gate"], to_node=n["doer"],
                 route=ROUTE_VERIFY_PASS),
        edge_cls(from_node=n["verifier_gate"], to_node=n["planner"],
                 route=ROUTE_VERIFY_REPLAN),
    ]


def _loop_edges(edge_cls, n) -> list:
    """doer → refiner → feedback → loop_gate ⟲, then validator → replan back
    to the planner, or done → learner."""
    from .graph_pipeline import (
        ROUTE_DONE, ROUTE_EXIT, ROUTE_LOOP, ROUTE_REPLAN)
    return [
        edge_cls(from_node=n["doer"], to_node=n["refiner"]),
        edge_cls(from_node=n["refiner"], to_node=n["feedback"]),
        edge_cls(from_node=n["feedback"], to_node=n["loop_gate"]),
        edge_cls(from_node=n["loop_gate"], to_node=n["doer"], route=ROUTE_LOOP),
        edge_cls(from_node=n["loop_gate"], to_node=n["validator"], route=ROUTE_EXIT),
        edge_cls(from_node=n["validator"], to_node=n["validator_gate"]),
        edge_cls(from_node=n["validator_gate"], to_node=n["planner"],
                 route=ROUTE_REPLAN),
        edge_cls(from_node=n["validator_gate"], to_node=n["learner"],
                 route=ROUTE_DONE),
    ]


def _workflow_concurrency() -> int | None:
    """Cap concurrent graph-scheduled nodes. The 4-way context fan-out against
    a single local mlx-lm endpoint is queueing, not parallelism — the server
    processes serially while 4 in-flight chat-mode prompts multiply KV-cache
    pressure (the ONE-117 OOM recipe). Floor is 3: with a smaller cap a replan
    pass can re-fire the 3 verify branches across two scheduler waves, and
    ADK's JoinNode then sees the not-yet-rescheduled third branch's stale
    pass-1 COMPLETED status and fires early with the old axis verdict
    (double-running merge_verdicts + verifier_gate). Raise for cloud providers
    via AIFORGE_WORKFLOW_MAX_CONCURRENCY (0 = unlimited)."""
    cap = int(os.environ.get("AIFORGE_WORKFLOW_MAX_CONCURRENCY", "3"))
    if cap <= 0:
        return None
    return max(3, cap)


def _unstall_chat_nodes(wf) -> None:
    """CRITICAL un-stall: ADK's graph builder CLONES every LlmAgent into the
    graph and forces wait_for_output=True for mode="chat" (conversational
    re-trigger semantics, _workflow_graph_utils.py). A chat node here never
    yields an engine "output" — its reply is message_as_output content — so the
    node parks in WAITING and downstream never triggers: the run stalled right
    after the enhancer. Our chat agents are one-shot graph stages; flip the flag
    on the CLONES (mutating the pre-construction originals is useless — the
    clone step overwrites it)."""
    from google.adk.agents import LlmAgent as _LlmAgent
    for node in wf.graph.nodes:
        if isinstance(node, _LlmAgent) and getattr(node, "mode", None) == "chat":
            node.wait_for_output = False


def _make_enhancer_guard():
    """After-callback for the ADK Enhancer: restore the RAW ask when the
    rewrite is degenerate (collapsed, or lost every named file/symbol) —
    everything downstream builds against ``enhanced_body``, so a bad rewrite
    poisons the whole run. ``ENHANCE_BLOCKED`` sentinels pass through
    untouched (that contract is handled by the runner)."""
    def _cb(callback_context=None, **_kw):
        try:
            st = getattr(callback_context, "state", None)
            if st is None:
                return None
            raw = st.get("raw_ask") or ""
            body = st.get("enhanced_body")
            if not raw or not isinstance(body, str):
                return None
            text = body.strip()
            if not text or text.startswith("ENHANCE_BLOCKED"):
                return None
            from .parallel_subtasks import _spec_degenerate
            bad = _spec_degenerate(raw, text)
            if bad:
                st["enhanced_body"] = raw
                logging.getLogger("aiforge.pipeline").warning(
                    "enhancer output rejected (%s) — raw ask restored", bad)
        except Exception:  # noqa: BLE001 — the guard must never break a run
            pass
        return None
    return _cb


def _append_callback(agent, attr: str, cb) -> None:
    """Append ``cb`` to ``agent.<attr>`` preserving existing callback(s).

    ADK accepts a single callable or a list, so every attach site had to
    re-derive the same three cases; they only ever differed in the attribute.
    """
    if cb is None:
        return
    existing = getattr(agent, attr, None)
    merged: list = []
    if existing is not None:
        merged.extend(existing if isinstance(existing, list) else [existing])
    merged.append(cb)
    setattr(agent, attr, merged)


def _append_after(agent, cb) -> None:
    _append_callback(agent, "after_agent_callback", cb)


def _append_before_model(agent, cb) -> None:
    _append_callback(agent, "before_model_callback", cb)


def build_live_verifier_agent(project: str | None = None):
    """Build the standalone live_verifier agent the runner invokes
    AFTER opening the PR. Runs on the operator's configured model (with
    the cloud escalation chain) like every other archetype."""
    return _live_verifier_mod.build(build_litellm_model, project=project)


__all__ = [
    "build_pipeline", "build_litellm_model", "build_live_verifier_agent",
    "set_force_provider", "get_force_provider",
]
