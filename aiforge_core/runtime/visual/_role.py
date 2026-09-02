"""Which model can actually SEE — and, when none can, why not.

The chat model on a local-only stack is usually text-only (qwen3-coder), so
every screenshot has to be translated into text by a separate vision-language
model wired to a dedicated role. The old caption path returned a bare ``""``
when that model was missing, which is indistinguishable from "the model looked
and had nothing to say" — the operator got silence and no way to find out that
vision was never configured. Every caller here gets a REASON instead.
"""
from __future__ import annotations

import os

# Role consulted when the caller's own model is text-only.
_DEFAULT_VISION_ROLE = "vision"


def _model_of(role: str) -> str:
    try:
        from aiforge_core.llm.router import resolve
        return (resolve(role).model or "").strip()
    except Exception:  # noqa: BLE001
        return ""


# Model ids that mean "nothing was configured here" (aiforge_core/config/env).
_PLACEHOLDERS = ("local-model-unconfigured", "default")


def _is_placeholder(model: str) -> bool:
    return model.strip().lower() in _PLACEHOLDERS


def _configured_role() -> str:
    return (os.environ.get("AIFORGE_VISION_ROLE") or _DEFAULT_VISION_ROLE).strip()


def _dedicated_role(role: str, vision_enabled) -> str | None:
    """The configured vision role, when it is really configured AND can see.

    Consulted BEFORE the caller's own model: ``vision_enabled`` honours a
    global ``vision_capable`` settings flag that is not per-role, so an
    operator who set it for a multimodal chat model and later switched chat to
    a text-only coder would otherwise have every screenshot sent to the coder
    while the VLM they configured sat unused.
    """
    cand = _configured_role()
    if cand == role:
        return None
    # Deliberately does NOT skip when the two roles name the same model: they
    # can point at DIFFERENT endpoints (the per-model base_url case), and a
    # probe that raised for the caller's role says nothing about this one.
    cand_model = _model_of(cand)
    if not cand_model or _is_placeholder(cand_model):
        return None
    try:
        return cand if vision_enabled(cand, probe=True) else None
    except Exception:  # noqa: BLE001 — a probe failure is not a verdict
        return None


def _no_vision_reason(role: str, cand: str) -> str:
    """Why no model can see — always naming the knob that fixes it."""
    if cand == role:
        return (f"the '{role}' model ({_model_of(role) or 'unset'}) does not "
                "accept images and AIFORGE_VISION_ROLE points back at it")
    try:
        from aiforge_core.llm.router import resolve
        ep = resolve(cand)
    except Exception:  # noqa: BLE001
        ep = None
    model = (getattr(ep, "model", "") or "").strip()
    if not model or _is_placeholder(model):
        return (f"no vision model configured: the '{role}' model "
                f"({_model_of(role) or 'unset'}) is text-only and role "
                f"'{cand}' has no model. Point it at a VLM (Settings → "
                f"Agents, or AIFORGE_{cand.upper()}_MODEL / "
                f"AIFORGE_{cand.upper()}_BASE_URL)")
    return (f"role '{cand}' is set to {model} but that endpoint rejected an "
            "image probe — pick a vision-capable model, or set its registry "
            "vision flag to 'yes' if the probe is wrong")


def vision_role(role: str = "chat") -> tuple[str | None, str]:
    """``(role_to_call, reason)``. ``reason`` is empty when a role was found.

    Capability comes from :mod:`vision_detect` (registry flag → live endpoint
    probe), never from a model-name allowlist: a local VLM is called whatever
    its operator named it, and ``qwen/qwen3.8-27b`` matches no published naming
    convention while happily accepting images.
    """
    try:
        from aiforge_core.runtime.vision_detect import vision_enabled
    except Exception as exc:  # noqa: BLE001
        return None, f"vision detection unavailable: {str(exc)[:120]}"

    dedicated = _dedicated_role(role, vision_enabled)
    if dedicated:
        return dedicated, ""
    try:
        if vision_enabled(role, probe=True):
            return role, ""
    except Exception:  # noqa: BLE001 — a probe failure is not a verdict
        pass
    return None, _no_vision_reason(role, _configured_role())


__all__ = ["vision_role"]
