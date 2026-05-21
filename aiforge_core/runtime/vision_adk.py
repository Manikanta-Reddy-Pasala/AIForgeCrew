"""ADK content-block bridge for the vision helper (sub #6 follow-up).

When a ticket has image attachments AND the active model supports vision,
the ADK ``LlmRequest.contents`` first user message is rewritten so the
image parts arrive alongside the text. Sub-call into :func:`vision.attach_image`
for the base64 encoding + mime sniff; this module owns the ADK shape only.

Used by ``adk_runner._run_pipeline`` as a one-shot pre-flight before the
SequentialAgent's first invocation.
"""
from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Any

from aiforge_core.runtime.sandbox import resolve_inside_root
from aiforge_core.runtime.vision import attach_image, supports_vision

log = logging.getLogger("aiforge.vision_adk")


def _read_image(rel_path: str) -> tuple[str | None, str | None]:
    """Return ``(data_url, error)``. Resolves ``rel_path`` against the
    repo root, sniffs mime, base64-encodes."""
    try:
        p = resolve_inside_root(rel_path)
    except PermissionError:
        return None, "path_traversal"
    if not p.is_file():
        return None, "not_found"
    blocks = attach_image("", p)
    if isinstance(blocks, dict):  # soft error returned
        return None, blocks.get("error")
    image_block = next(
        (b for b in blocks if b.get("type") == "image_url"), None,
    )
    if image_block is None:
        return None, "no_image_block"
    return image_block["image_url"]["url"], None


def inject_image_parts(
    contents: list[Any],
    model_id: str,
    image_paths: list[str],
) -> list[Any]:
    """Return a NEW contents list with image parts appended to the first
    user message. No-op when ``supports_vision(model_id)`` is False or
    ``image_paths`` is empty.
    """
    if not image_paths or not supports_vision(model_id):
        return list(contents)
    if not contents:
        return list(contents)

    try:
        from google.genai import types as gtypes
    except ImportError:
        return list(contents)

    out = list(contents)
    head = out[0]
    head_role = getattr(head, "role", "") or "user"
    if head_role != "user":
        return out

    head_parts = list(getattr(head, "parts", []) or [])
    appended = 0
    for rel in image_paths:
        data_url, err = _read_image(rel)
        if data_url is None:
            log.warning("vision.skip path=%s error=%s", rel, err)
            continue
        # Strip the data URL prefix and feed Part.from_bytes when ADK
        # supports it; otherwise fall back to a text annotation so the
        # information survives.
        try:
            mime_and_b64 = data_url.split(",", 1)
            mime = mime_and_b64[0].split(";")[0].removeprefix("data:")
            raw = base64.b64decode(mime_and_b64[1])
            part = gtypes.Part.from_bytes(data=raw, mime_type=mime)
        except Exception as exc:  # noqa: BLE001 — best-effort
            log.debug("vision.part_build_failed path=%s: %s", rel, exc)
            part = gtypes.Part.from_text(
                text=f"[image attachment unavailable: {rel}]",
            )
        head_parts.append(part)
        appended += 1

    if appended == 0:
        return out
    new_head = gtypes.Content(role="user", parts=head_parts)
    out[0] = new_head
    log.info("vision.inject count=%d model=%s", appended, model_id)
    return out


__all__ = ["inject_image_parts"]
