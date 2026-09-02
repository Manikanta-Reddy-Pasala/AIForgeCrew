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


def vision_role(role: str = "chat") -> tuple[str | None, str]:
    """``(role_to_call, reason)``. ``reason`` is empty when a role was found.

    Order: the caller's own model if it accepts images, else the dedicated
    vision role. Capability comes from :mod:`vision_detect` (registry flag →
    live endpoint probe), never from a model-name allowlist: a local VLM is
    called whatever its operator named it, and ``qwen/qwen3.8-27b`` matches no
    published naming convention while happily accepting images.
    """
    try:
        from aiforge_core.runtime.vision_detect import vision_enabled
    except Exception as exc:  # noqa: BLE001
        return None, f"vision detection unavailable: {str(exc)[:120]}"

    # A DEDICATED vision role, when one is really configured, is consulted
    # BEFORE the caller's own model. Deliberate: ``vision_enabled`` honours a
    # global ``vision_capable`` settings flag that is not per-role, so an
    # operator who once set it for a multimodal chat model and later switched
    # chat to a text-only coder would otherwise have every screenshot sent to
    # the coder — which cannot see — while the VLM they configured sat unused.
    cand = _configured_role()
    if cand != role:
        cand_model = _model_of(cand)
        if cand_model and not _is_placeholder(cand_model) \
                and cand_model != _model_of(role):
            try:
                if vision_enabled(cand, probe=True):
                    return cand, ""
            except Exception:  # noqa: BLE001
                pass

    try:
        if vision_enabled(role, probe=True):
            return role, ""
    except Exception:  # noqa: BLE001 — a probe failure is not a verdict
        pass

    if cand == role:
        return None, (
            f"the '{role}' model ({_model_of(role) or 'unset'}) does not accept "
            "images and AIFORGE_VISION_ROLE points back at it")

    try:
        from aiforge_core.llm.router import resolve
        ep = resolve(cand)
    except Exception:  # noqa: BLE001
        ep = None
    if ep is None or not (getattr(ep, "model", "") or "").strip():
        return None, (
            f"no vision model configured: the '{role}' model "
            f"({_model_of(role) or 'unset'}) is text-only and role '{cand}' has "
            "no model. Point it at a VLM (Settings → Agents, or "
            f"AIFORGE_{cand.upper()}_MODEL / AIFORGE_{cand.upper()}_BASE_URL)")

    try:
        if vision_enabled(cand, probe=True):
            return cand, ""
    except Exception:  # noqa: BLE001
        pass
    return None, (
        f"role '{cand}' is set to {ep.model} but that endpoint rejected an "
        "image probe — pick a vision-capable model, or set its registry vision "
        "flag to 'yes' if the probe is wrong")


__all__ = ["vision_role"]
