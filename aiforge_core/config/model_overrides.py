"""Per-model request customizations ("model quirk sheet").

Some local models need request-level tuning to behave — e.g.
qwen3_5_moe-family models (nex-n2-mini, qwen3.5-122b) stochastically
loop in their reasoning channel until the token ceiling unless the
system prompt forbids deliberation (empirically validated 2026-06-13:
drown rate 2/3 -> 1/8, see llm-bench REPORT). The chat-template knobs
(``enable_thinking``/``/no_think``) are ignored by these templates, so
prompt + budget is the only working lever.

The registry is keyed by case-insensitive substring match on the model
id. Overrides are applied by ``EscalatingLlm`` right before each
attempt is forwarded, so they follow the request to whichever model
actually serves it (primary or chain entry) and never leak one model's
quirks onto another.

Operator extension without code changes:
``$AIFORGE_CONFIG_DIR/model_overrides.json`` — same shape as
``_BUILTIN`` below, merged on top (file wins on key collision).

Override keys:

* ``system_suffix``    appended to the request's system instruction
* ``max_output_tokens`` cap, applied only when the request doesn't
                        already carry a SMALLER cap
* ``temperature``      forced sampling temperature
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger("aiforge.model_overrides")

NO_REASONING_SUFFIX = (
    "Respond with the final answer immediately. Do NOT deliberate, "
    "plan, or reason step by step — internal thinking is forbidden. "
    "Output the answer only."
)

# Validated against llm-bench R1 (2026-06-13): recipe = suppression
# prompt + 2500-token cap; good answers fit in <1.5K tokens and a
# reasoning-loop run dies in ~30s instead of 87s, where the
# EscalatingLlm empty-response judge catches it and retries.
_QWEN35_MOE_OVERRIDE: dict[str, Any] = {
    "system_suffix": NO_REASONING_SUFFIX,
    "max_output_tokens": 2500,
    "temperature": 0.1,
}

# Substring (lowercased) -> override dict. First match wins; order
# matters for overlapping patterns, so keep the more specific first.
_BUILTIN: dict[str, dict[str, Any]] = {
    "nex-n2-mini": _QWEN35_MOE_OVERRIDE,
    "qwen3.5-122b": _QWEN35_MOE_OVERRIDE,
    # qwen3-coder-next: thoroughness nudge for judge-style asks is
    # handled at the prompt layer (role prompts), not here — the model
    # has no reasoning channel to suppress and benefits from defaults.
}


def _file_overrides() -> dict[str, dict[str, Any]]:
    root = Path(os.environ.get("AIFORGE_CONFIG_DIR",
                               os.path.expanduser("~/.aiforge")))
    p = root / "model_overrides.json"
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
        if isinstance(data, dict):
            return {str(k).lower(): v for k, v in data.items()
                    if isinstance(v, dict)}
    except Exception as exc:  # noqa: BLE001
        log.warning("model_overrides.json unreadable: %s", exc)
    return {}


def lookup(model_id: str | None) -> dict[str, Any] | None:
    """Return the override dict for *model_id*, or None."""
    if not model_id:
        return None
    needle = model_id.lower()
    merged = dict(_BUILTIN)
    merged.update(_file_overrides())
    for pattern, override in merged.items():
        if pattern in needle:
            return override
    return None


# Roles that emit long, structured output (file contents, full plans,
# multi-step diffs). The anti-think recipe was validated on SHORT-answer
# judges (triage/verify/feedback/validator); its 2500-token cap truncates
# a doer's file-write tool-call args mid-string ("repaired malformed
# tool-call args" → broken files), and its "thinking forbidden" suffix
# kneecaps a coding/planning role. So skip the override entirely for these
# when the SAME model is also serving a judge — the cap only ever made
# sense per-judge-role, not per-model.
_GENERATIVE_ROLES: frozenset[str] = frozenset({
    "doer", "refiner", "planner", "enhancer", "architect",
    "learner", "researcher",
})


def apply(model_id: str | None, llm_request, role: str | None = None):
    """Return *llm_request* with the model's overrides applied.

    Returns the original request untouched when no override matches OR
    when *role* is a long-output generative role (the recipe is for
    short-answer judges only). Never raises — a broken override must not
    kill the pipeline.
    """
    if role and role.lower() in _GENERATIVE_ROLES:
        return llm_request
    override = lookup(model_id)
    if not override:
        return llm_request
    try:
        req = llm_request.model_copy(deep=True)
        cfg = req.config
        suffix = override.get("system_suffix")
        if suffix:
            existing = cfg.system_instruction or ""
            if suffix not in str(existing):
                cfg.system_instruction = (
                    f"{existing}\n\n{suffix}".strip())
        cap = override.get("max_output_tokens")
        if cap and (not cfg.max_output_tokens
                    or cfg.max_output_tokens > cap):
            cfg.max_output_tokens = cap
        if override.get("temperature") is not None:
            cfg.temperature = override["temperature"]
        log.debug("model_overrides.applied model=%s keys=%s",
                  model_id, sorted(override))
        return req
    except Exception as exc:  # noqa: BLE001
        log.warning("model_overrides.apply failed for %s: %s",
                    model_id, exc)
        return llm_request


__all__ = ["apply", "lookup", "NO_REASONING_SUFFIX"]
