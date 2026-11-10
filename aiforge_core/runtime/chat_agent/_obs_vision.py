"""Attach screenshot pixels to an OBSERVATION when the chat model can see.

``browse`` / ``ui_check`` used to strip the PNG and replace it with a caption
from a second vision model, because the chat path was treated as text-only.
When the chat model is multimodal, the same turn should carry the image —
caption stays as a short text fallback for text-only models.
"""
from __future__ import annotations


def chat_can_see(*, role: str = "chat") -> bool:
    try:
        from aiforge_core.runtime.vision_detect import vision_enabled
        return bool(vision_enabled(role, probe=True))
    except Exception:  # noqa: BLE001
        return False


def mark_pixels(result: dict, path: str | None, *, role: str = "chat") -> dict:
    """Flag a tool result so the observation step attaches ``path`` as pixels.

    Soft-fail: never raises; leaves the result unchanged when vision is off.
    """
    if not isinstance(result, dict) or not path:
        return result
    if not chat_can_see(role=role):
        return result
    result["screenshot"] = path
    result["pixels"] = True
    return result


def observation_content(obs: str, result) -> str | list:
    """User-turn content for a tool observation — multimodal when flagged."""
    text = f"OBSERVATION: {obs}"
    if not isinstance(result, dict) or not result.get("pixels"):
        return text
    path = result.get("screenshot") or ""
    if not path:
        return text
    try:
        from aiforge_core.runtime import vision
        blocks = vision.attach_image(text, path)
    except Exception:  # noqa: BLE001
        return text
    if isinstance(blocks, list):
        return blocks
    return text


__all__ = ["chat_can_see", "mark_pixels", "observation_content"]
