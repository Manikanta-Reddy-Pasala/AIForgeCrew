"""Multimodal vision helpers (sub #6).

Convert local image files into LiteLLM-shaped multimodal content blocks
so screenshots from the :mod:`browser` tool or ticket-attached images
can flow into the next LLM call. Static allowlist gates the toggle —
non-vision models silently ignore the image and receive text-only input
elsewhere in the pipeline.
"""
from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

_MAX_BYTES = 5 * 1024 * 1024  # 5 MB

_VISION_MODELS = (
    # OpenAI
    "gpt-4-vision", "gpt-4o", "gpt-4.1", "gpt-4-turbo",
    # Google
    "gemini-pro-vision", "gemini-1.5", "gemini-2",
    # Qwen multimodal
    "qwen2-vl", "qwen-vl",
)

# Magic byte signatures for common formats.
_MIME_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"RIFF", "image/webp"),  # bytes 0-3; bytes 8-11 should be "WEBP"
)


def _detect_mime(raw: bytes) -> str | None:
    for sig, mime in _MIME_SIGNATURES:
        if raw.startswith(sig):
            if mime == "image/webp" and len(raw) < 12:
                return None
            if mime == "image/webp" and raw[8:12] != b"WEBP":
                return None
            return mime
    return None


def supports_vision(model_id: str) -> bool:
    """Return True when ``model_id`` is on the multimodal allowlist."""
    if not model_id:
        return False
    low = model_id.lower()
    return any(token in low for token in _VISION_MODELS)


def attach_image(
    text: str, image_path: str | Path,
) -> list[dict[str, Any]] | dict[str, Any]:
    """Return LiteLLM-shaped multimodal content blocks for ``image_path``
    or ``{ok: False, error}`` on failure (soft-error contract).
    """
    p = Path(image_path)
    if not p.is_file():
        return {"ok": False, "error": "not_found", "path": str(image_path)}
    raw = p.read_bytes()
    if len(raw) > _MAX_BYTES:
        return {"ok": False, "error": "image_too_large",
                "bytes": len(raw), "limit": _MAX_BYTES}
    mime = _detect_mime(raw)
    if mime is None:
        return {"ok": False, "error": "unsupported_format",
                "path": str(image_path)}
    b64 = base64.b64encode(raw).decode("ascii")
    return [
        {"type": "text", "text": text},
        {
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{b64}"},
        },
    ]


__all__ = ["attach_image", "supports_vision"]
