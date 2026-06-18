"""Parallel graph stages for the native ADK ``Workflow``.

Two fan-outs replace what were single sequential agents. In the
``Workflow`` model a fan-out is N edges from one source to N branch
nodes; the branches converge at a ``JoinNode`` (waits for all), then a
``FunctionNode`` merges their per-branch state into one key:

* **context** — researcher + ctx_repomap + ctx_conventions
  run in parallel; :func:`merge_context` concatenates their ``*_brief_md``
  keys into ``context_brief_md`` for the Doer.
* **verifier** — verify_correctness + verify_scope + verify_risk run in
  parallel; :func:`merge_verdicts` ANDs their JSON verdicts into the
  legacy ``verifier_verdict`` dict (reject if ANY axis rejects) so the
  runner's ``_extract_verifier`` keeps working unchanged.

The merge bodies are wrapped as ``FunctionNode`` via the ``make_*``
factories. They read/write ``ctx.state`` directly — the workflow engine
flushes those mutations as state deltas.
"""
from __future__ import annotations

import json
from typing import Any

from aiforge_core.agents import (
    ctx_conventions as _ctx_conventions_mod,
)
from aiforge_core.agents import (
    ctx_repomap as _ctx_repomap_mod,
)
from aiforge_core.agents import (
    researcher as _researcher_mod,
)
from aiforge_core.agents import (
    verify_correctness as _verify_correctness_mod,
)
from aiforge_core.agents import (
    verify_risk as _verify_risk_mod,
)
from aiforge_core.agents import (
    verify_scope as _verify_scope_mod,
)

# (state_key, human heading) for each context brief, in display order.
# NOTE: memory_brief_md is NOT merged here — it is injected directly via
# {memory_brief_md?} in the doer/planner/enhancer/verify_risk prompts so
# the TRIVIAL fast-path (which skips this merge node entirely) still
# carries memory, and the full path doesn't get a second folded copy.
_CONTEXT_BRIEFS: list[tuple[str, str]] = [
    ("research_brief_md", "Researcher"),
    ("repo_brief_md", "Repo map"),
    ("conventions_brief_md", "Conventions"),
]

_VERIFY_AXES: list[tuple[str, str]] = [
    ("verify_correctness", "correctness"),
    ("verify_scope", "scope"),
    ("verify_risk", "risk"),
]


def _coerce_verdict(raw: Any) -> dict:
    """Normalise a sub-verifier output into a verdict dict.

    Accepts a dict, a JSON string, or a bare verdict token. Anything
    unparseable is treated as ``pass`` — a parse failure must not block
    the pipeline on a critic's formatting slip; the other two axes plus
    the downstream Feedback/Validator gates still apply.
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text[:4].lower() == "json":
                text = text[4:]
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                return obj
        except Exception:
            head = text.lstrip("`*_-> ").lower()
            if head.startswith("reject"):
                return {"verdict": "reject", "rationale": text[:200]}
    return {"verdict": "pass"}


# ── merge node bodies (FunctionNode) ────────────────────────────────────

async def merge_context(ctx):  # type: ignore[no-untyped-def]
    """Concat the per-gatherer briefs into ``context_brief_md``."""
    try:
        state = ctx.state
        sections: list[str] = []
        for key, heading in _CONTEXT_BRIEFS:
            val = state.get(key)
            if isinstance(val, str) and val.strip():
                sections.append(f"## {heading}\n\n{val.strip()}")
        if sections:
            state["context_brief_md"] = "\n\n".join(sections)
    except Exception:
        pass  # never break the graph on a merge slip


async def merge_verdicts(ctx):  # type: ignore[no-untyped-def]
    """AND the three axis verdicts into the legacy ``verifier_verdict``."""
    try:
        state = ctx.state
        issues: list = []
        rejected = False
        reject_axes: list[str] = []
        for key, axis in _VERIFY_AXES:
            verdict = _coerce_verdict(state.get(key))
            if str(verdict.get("verdict", "pass")).lower() == "reject":
                rejected = True
                reject_axes.append(axis)
                rat = verdict.get("rationale")
                if rat:
                    issues.append({"kind": axis, "message": str(rat)})
            for it in verdict.get("issues") or []:
                issues.append(it)
        state["verifier_verdict"] = {
            "verdict": "reject" if rejected else "pass",
            "issues": issues,
            "rationale": (
                f"rejected by: {', '.join(reject_axes)}" if rejected
                else "all axes passed"
            ),
        }
    except Exception:
        pass


async def research_entry(ctx):  # type: ignore[no-untyped-def]
    """No-op fan-out source for the context branches.

    Exists so the context fan-out has a single stable re-entry point:
    the first pass enters from the Enhancer, the research-gap loop
    re-enters here. Re-entering one node re-fires ALL outgoing branch
    edges in one scheduler wave, which is what context_join needs to
    re-arm (a JoinNode fired with only a subset of its in-branches
    rescheduled reads stale COMPLETED status — the ONE-117
    max_concurrency note in pipeline.py). Body intentionally does
    nothing."""
    return None


# ── builders ────────────────────────────────────────────────────────────

def build_context_branches(model_factory, *, skip_researcher: bool = False,
                           skip_conventions: bool = False):
    """Return the parallel context-gatherer agent nodes (chat-mode).

    ``skip_conventions`` drops the ctx_conventions LLM branch — used when
    the target repo carries glob-scoped rules files (.aiforge/rules /
    .cursor/rules / AGENTS.md), which provide the conventions for free.
    """
    branches: list = []
    if not skip_researcher:
        branches.append(_researcher_mod.build(model_factory))
    # ctx_memory was REMOVED from the fan-out (2026-06-11 efficiency
    # audit): it was an LLM agent re-querying the exact backends the
    # runner's pre-flight memory_block already queried. The pre-flight
    # result seeds state['memory_brief_md'], injected DIRECTLY into the
    # doer/planner/enhancer/verify_risk prompts (not merged here — the
    # trivial path skips this merge node).
    branches.append(_ctx_repomap_mod.build(model_factory))
    if not skip_conventions:
        branches.append(_ctx_conventions_mod.build(model_factory))
    return branches


def build_verifier_branches(model_factory):
    """Return the three parallel sub-verifier agent nodes (chat-mode)."""
    return [
        _verify_correctness_mod.build(model_factory),
        _verify_scope_mod.build(model_factory),
        _verify_risk_mod.build(model_factory),
    ]


def make_context_join():
    from google.adk.workflow import JoinNode
    return JoinNode(name="context_join")


def make_verifier_join():
    from google.adk.workflow import JoinNode
    return JoinNode(name="verifier_join")


def make_merge_context_node():
    from google.adk.workflow import node
    return node(merge_context, name="merge_context")


def make_merge_verdicts_node():
    from google.adk.workflow import node
    return node(merge_verdicts, name="merge_verdicts")


def make_research_entry_node():
    from google.adk.workflow import node
    return node(research_entry, name="research_entry")


__all__ = [
    "merge_context",
    "merge_verdicts",
    "research_entry",
    "build_context_branches",
    "build_verifier_branches",
    "make_context_join",
    "make_verifier_join",
    "make_merge_context_node",
    "make_merge_verdicts_node",
    "make_research_entry_node",
]
