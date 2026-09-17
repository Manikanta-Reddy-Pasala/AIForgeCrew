"""Suggesting and assigning models to roles."""
from __future__ import annotations


def _pkg():
    """``model_registry``, the module this code was split from, looked up on each call.

    Only names read through here follow a patch on ``model_registry``; patch any other
    name on this module."""
    import aiforge_core.config.model_registry as package
    return package


# Roles that benefit from a reasoning/"thinking" model (deep planning/judging).
_THINKING_ROLES = ("planner", "architect", "reviewer",
                   "validator", "critic", "reasoner", "judge", "orchestrator",
                   "gap_eval", "verify",
                   # memory = distil/consolidate. Judging what is durable and
                   # what supersedes what needs reasoning; the fast role is what
                   # let fragments and raw chat turns through as "facts".
                   "memory")
# QUICK, direct-output roles — a reasoning/"thinking" model is WRONG here: it
# spends its whole budget thinking and returns EMPTY on these short tasks
# (rephrase a query, distil a fact, classify, title). Force the fast
# NON-thinking model. (enhancer/learner were mis-classified as thinking —
# that's what made them return empty on a reasoning model.)
_FAST_ROLES = ("enhancer", "learner", "triage", "feedback", "refiner",
               "title", "summar", "classif", "ctx_", "live_verifier")
# Code-generation-heavy roles — a fast non-reasoning coder is better + cheaper.
_CODER_ROLES = ("doer", "developer", "coder", "implementer", "builder", "tester")


def is_fast_role(role: str) -> bool:
    """True when ``role`` is a QUICK, direct-output role (enhancer/learner/
    triage/feedback/refiner/title/summary/classify/…). These want a plain answer,
    NOT a reasoning trace — a reasoning model spends its budget thinking and
    returns empty. Callers use this to pre-empt the reasoning phase (send
    ``/no_think`` from the first attempt) so a fast role never wastes a round on
    an empty reasoning-model response."""
    rl = (role or "").strip().lower()
    return any(f in rl for f in _FAST_ROLES)


# Embedding / rerank models can't generate — never assign them to a chat role.
_NON_GENERATIVE_MARKERS = (
    "embed", "embedding", "rerank", "reranker", "bge-", "-bge", "nomic-embed",
    "gte-", "e5-", "instructor", "sentence-transformer",
)


def _is_generative(model_id: str) -> bool:
    m = (model_id or "").lower()
    return not any(k in m for k in _NON_GENERATIVE_MARKERS)


def _by_ctx(ms):
    """Models sorted by DESCENDING context window (largest first)."""
    return sorted(ms, key=lambda m: -(m.get("context_window") or 0))


def _assign_role(rl, vision, coder, think, models, default):
    """Best model id for one role name by capability: vision-needing -> vision,
    fast/direct -> non-thinking coder, thinking -> reasoning, coder -> coder,
    else the fast default."""
    if "vision" in rl and vision:
        return vision[0]["id"]
    if any(f in rl for f in _FAST_ROLES):
        return (coder or think or _by_ctx(models))[0]["id"]
    if any(t in rl for t in _THINKING_ROLES) and think:
        return think[0]["id"]
    if any(c in rl for c in _CODER_ROLES) and coder:
        return coder[0]["id"]
    return default


def suggest_assignments(roles: list) -> dict:
    """Map each role to the best available model BY CAPABILITY: thinking roles →
    a reasoning model, coder roles → a fast non-reasoning coder, vision-needing →
    a vision model. Larger context wins within a tier. {role: model_id}.
    Embedding/rerank models are excluded — they can't generate."""
    models = [m for m in _pkg().list_models() if _is_generative(m.get("model") or m.get("id"))]
    if not models:
        return {}
    think = _by_ctx([m for m in models if m.get("has_thinking")])
    coder = _by_ctx([m for m in models if not m.get("has_thinking")])
    vision = _by_ctx([m for m in models if m.get("has_vision")])
    # DEFAULT for unclassified roles (e.g. chat) = the FAST non-thinking model,
    # not the largest-context one — a reasoning model as the blanket default is
    # what silently made simple chat answers come back empty. Only fall to a
    # thinking model when no fast one is configured.
    default = (coder or think or _by_ctx(models))[0]["id"]
    out: dict = {}
    for role in roles:
        out[role] = _assign_role((role or "").lower(), vision, coder, think,
                                 models, default)
    return out


def auto_assign(roles: list) -> dict:
    """Compute + APPLY capability-based assignments for ``roles``. Groups roles by
    chosen model and writes each into agent_config. Returns the plan + results."""
    pkg = _pkg()
    plan = pkg.suggest_assignments(roles)
    by_model: dict = {}
    for role, mid in plan.items():
        by_model.setdefault(mid, []).append(role)
    results = {mid: pkg.apply_to_roles(mid, rs) for mid, rs in by_model.items()}
    return {"assignments": plan, "results": results}
