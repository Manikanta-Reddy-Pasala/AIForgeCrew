"""ParallelAgent stages for the v6 pipeline + their merge callbacks.

Two fan-outs replace what used to be single sequential agents:

* **context_gather** — ``ParallelAgent`` running the Researcher plus the
  three concurrent gatherers (ctx_memory / ctx_repomap / ctx_conventions).
  Each writes its own ``*_brief_md`` key; :func:`merge_context_briefs`
  concatenates them into ``context_brief_md`` for the Doer.

* **verifier** — ``ParallelAgent`` running the three sub-verifiers
  (verify_correctness / verify_scope / verify_risk). Each emits a JSON
  verdict; :func:`merge_verifier_verdicts` ANDs them into the legacy
  ``verifier_verdict`` dict (reject if ANY axis rejects) so the runner's
  ``_extract_verifier`` keeps working unchanged.

The merge callbacks are plain ``after_agent_callback`` hooks — pure,
state-only, and exception-safe so a flaky branch never breaks the run.
"""
from __future__ import annotations

import json
from typing import Any

from aiforge_core.agents import (
    ctx_conventions as _ctx_conventions_mod,
)
from aiforge_core.agents import (
    ctx_memory as _ctx_memory_mod,
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
_CONTEXT_BRIEFS: list[tuple[str, str]] = [
    ("research_brief_md", "Researcher"),
    ("memory_brief_md", "Memory"),
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
        # strip a ```json fence if present
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


async def merge_context_briefs(*, callback_context, **_kw):  # type: ignore[no-untyped-def]
    """ParallelAgent after-callback — concat the per-gatherer briefs into
    ``context_brief_md`` so the Doer reads one consolidated block."""
    try:
        state = callback_context.state
        sections: list[str] = []
        for key, heading in _CONTEXT_BRIEFS:
            val = state.get(key)
            if isinstance(val, str) and val.strip():
                sections.append(f"## {heading}\n\n{val.strip()}")
        if sections:
            state["context_brief_md"] = "\n\n".join(sections)
    except Exception:
        pass  # never break the pipeline on a merge slip
    return None


async def merge_verifier_verdicts(*, callback_context, **_kw):  # type: ignore[no-untyped-def]
    """ParallelAgent after-callback — AND the three axis verdicts into the
    legacy ``verifier_verdict`` dict. Reject if ANY axis rejects."""
    try:
        state = callback_context.state
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
        merged = {
            "verdict": "reject" if rejected else "pass",
            "issues": issues,
            "rationale": (
                f"rejected by: {', '.join(reject_axes)}" if rejected
                else "all axes passed"
            ),
        }
        state["verifier_verdict"] = merged
    except Exception:
        pass  # leave verifier_verdict unset; runner treats None as non-blocking
    return None


def build_context_parallel(model_factory, *, skip_researcher: bool = False):
    """Build the ``ParallelAgent`` context-gathering stage.

    Runs the three concurrent gatherers (plus the Researcher unless
    ``skip_researcher``) and merges their briefs into ``context_brief_md``.
    """
    from google.adk.agents import ParallelAgent

    branches: list = []
    if not skip_researcher:
        branches.append(_researcher_mod.build(model_factory))
    branches.append(_ctx_memory_mod.build(model_factory))
    branches.append(_ctx_repomap_mod.build(model_factory))
    branches.append(_ctx_conventions_mod.build(model_factory))

    stage = ParallelAgent(
        name="context_gather",
        sub_agents=branches,
        after_agent_callback=merge_context_briefs,
    )
    return stage


def build_verifier_parallel(model_factory):
    """Build the ``ParallelAgent`` verifier stage — three axis critics
    merged into ``verifier_verdict``."""
    from google.adk.agents import ParallelAgent

    branches = [
        _verify_correctness_mod.build(model_factory),
        _verify_scope_mod.build(model_factory),
        _verify_risk_mod.build(model_factory),
    ]
    stage = ParallelAgent(
        name="verifier",
        sub_agents=branches,
        after_agent_callback=merge_verifier_verdicts,
    )
    return stage


__all__ = [
    "merge_context_briefs",
    "merge_verifier_verdicts",
    "build_context_parallel",
    "build_verifier_parallel",
]
