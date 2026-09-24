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

from ._pipeline_graph import (  # noqa: F401  # re-exported
    _append_after,
    _append_before_model,
    _append_callback,
    _attach_agent_callbacks,
    _context_edges,
    _entry_edges,
    _loop_edges,
    _make_enhancer_guard,
    _plan_edges,
    _repair_enhanced_body,
    _set_node_modes,
    _set_node_retries,
    _unstall_chat_nodes,
    _workflow_concurrency,
)
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
       swap to ``cloud_default_for_local`` (an operator-pinned cloud
       provider; none by default) so the agent loop doesn't pay
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
    _attach_tool_guards(doer)
    return doer


def _attach_tool_guards(agent, callbacks=None):
    """Attach tool-boundary guards to ``agent``.

    Any agent that can reach a shell needs the SAFETY ones, not just the Doer —
    which is how ``live_verifier`` (it holds ``bash`` and runs unattended after
    the PR is rolled out) ended up executing model-composed commands with no
    risk verdict, no operator ``deny`` policy and no PreToolUse hook applied to
    it. Each attach is guarded: a guard never blocks pipeline boot."""
    for attr, factory in (callbacks or _DOER_TOOL_CALLBACKS):
        try:
            _append_callback(agent, attr, factory())
        except Exception:  # noqa: BLE001 — a guard never blocks pipeline boot
            pass
    return agent


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

# What EVERY shell-capable agent gets, Doer or not: the scope guard, the
# risk/policy/approval gate and the operator's own hooks. The repeat guard and
# the quality-signal recorder are Doer concerns and are left out on purpose
# (see build_live_verifier_agent).
_SHELL_AGENT_TOOL_CALLBACKS = (
    ("before_tool_callback", _scope_guard_cb),
    ("before_tool_callback", _approval_gate_cb),
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
    # unused, deliberately: the recipe is baked into live_verifier, not the graph build.
    del project
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


# The roles delegate_to_agent may run on their own.
_DELEGABLE = frozenset({"researcher", "planner", "refiner", "triage", "verifier"})


def build_role_agent(role: str):
    """ONE role's agent, built standalone — what ``delegate_to_agent`` runs.

    The delegate used to build the whole pipeline and look the role up in its
    ``sub_agents``; the pipeline is a Workflow graph now and has none, so every
    delegation returned ``delegate_build_failed`` while the tool was still
    offered to the Doer and to chat. Same safety guards as the live verifier
    (risk/policy gate, scope, operator hooks). None for any other role."""
    role = (role or "").strip().lower()
    if role not in _DELEGABLE:
        return None
    import importlib
    mod = importlib.import_module(f"aiforge_core.agents.{role}")
    return _attach_tool_guards(mod.build(build_litellm_model),
                               callbacks=_SHELL_AGENT_TOOL_CALLBACKS)


def build_live_verifier_agent(project: str | None = None):
    """Build the standalone live_verifier agent the runner invokes
    AFTER opening the PR. Runs on the operator's configured model (with
    the cloud escalation chain) like every other archetype.

    Carries the SAFETY guards (risk/policy gate, scope, operator hooks). It
    runs unattended against a deployed environment with ``bash`` in hand, so it
    is the last agent that should be the one without a risk gate.

    Deliberately WITHOUT the repeat guard: verification legitimately re-runs
    the identical command — poll the health endpoint, check again after the
    rollout settles — and blocking the 4th identical call would break the one
    thing this agent exists to do."""
    return _attach_tool_guards(
        _live_verifier_mod.build(build_litellm_model, project=project),
        callbacks=_SHELL_AGENT_TOOL_CALLBACKS)


__all__ = [
    "build_pipeline", "build_litellm_model", "build_live_verifier_agent",
    "build_role_agent",
    "set_force_provider", "get_force_provider",
]
