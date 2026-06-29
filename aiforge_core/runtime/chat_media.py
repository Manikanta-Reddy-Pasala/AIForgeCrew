"""Chat image attachments: per-session storage + descriptions + context.

A user can attach images to a chat session. Each image is saved under the
session's own media folder and given a DESCRIPTION (auto-captioned when the
session's model is vision-capable, otherwise a user-typed caption). The
descriptions are injected into every turn's context as a "SESSION IMAGES"
block, so the (possibly text-only) chat model can answer questions about the
images throughout the session. When the model IS vision-capable the actual
image is also passed on the turn.

Vision capability is auto-detected from the model id (``vision.supports_vision``)
and can be force-enabled in settings (``runtime_settings`` ``vision_capable``)
for a self-hosted multimodal model the allowlist doesn't recognise.

The descriptions live only in the live context — they are NOT folded into the
session-summary markdown or auto-memory (chat_persist), so the session summary
stays clean.
"""
from __future__ import annotations

import os
from pathlib import Path

from aiforge_core.runtime import vision

_MAX_BYTES = 5 * 1024 * 1024     # mirror vision._MAX_BYTES


def _config_root() -> str:
    return os.path.expanduser(os.environ.get("AIFORGE_CONFIG_DIR", "~/.aiforge"))


def media_dir(session_id: int) -> str:
    """Per-session image folder. Lives under the chat-workspaces tree so it
    rides the same persisted volume as the rest of a session's state."""
    root = os.environ.get(
        "AIFORGE_CHAT_WORKSPACE_ROOT",
        os.path.join(_config_root(), "chat-workspaces"))
    d = os.path.join(root, f"session-{session_id}", ".aiforge", "media")
    os.makedirs(d, exist_ok=True)
    return d


def _safe_name(name: str) -> str:
    base = os.path.basename(name or "image")
    return "".join(ch for ch in base if ch.isalnum() or ch in "._-") or "image"


def save_image(session_id: int, filename: str, raw: bytes) -> dict:
    """Validate + write an uploaded image to the session's media folder.
    Returns ``{ok, path, mime, filename}`` or ``{ok: False, error}``."""
    if len(raw) > _MAX_BYTES:
        return {"ok": False, "error": "image_too_large",
                "bytes": len(raw), "limit": _MAX_BYTES}
    mime = vision._detect_mime(raw)
    if mime is None:
        return {"ok": False, "error": "unsupported_format"}
    name = _safe_name(filename)
    # Avoid clobbering same-named uploads in one session.
    dest = os.path.join(media_dir(session_id), name)
    stem, ext = os.path.splitext(dest)
    n = 1
    while os.path.exists(dest):
        dest = f"{stem}_{n}{ext}"
        n += 1
    Path(dest).write_bytes(raw)
    return {"ok": True, "path": dest, "mime": mime,
            "filename": os.path.basename(dest)}


def vision_enabled(role: str = "chat") -> bool:
    """True when the session's model can see images — auto-detected from the
    resolved model id OR force-enabled via the ``vision_capable`` setting."""
    try:
        from aiforge_core.config import runtime_settings
        if int(runtime_settings.get("vision_capable")) > 0:
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        from aiforge_core.llm.router import resolve
        return vision.supports_vision(resolve(role).model or "")
    except Exception:  # noqa: BLE001
        return False


def describe_image(path: str, role: str = "chat") -> str:
    """Auto-caption an image with the vision model. Best-effort — returns ""
    when vision is unavailable or the call fails (caller falls back to a
    user-typed caption)."""
    if not vision_enabled(role):
        return ""
    content = vision.attach_image(
        "Describe this image concisely (1-2 sentences) so it can be referenced "
        "later in the conversation. Note any visible text, UI, chart, or code.",
        path)
    if isinstance(content, dict):        # soft-error from attach_image
        return ""
    try:
        from aiforge_core.llm.client import complete
        out = complete(role, [{"role": "user", "content": content}],
                       max_tokens=200)
        return (out or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def context_block(session_id: int) -> str:
    """The "SESSION IMAGES" text injected into every turn so the model can
    answer questions about uploaded images even when it can't see them."""
    from aiforge_core.runtime import chat_store
    rows = chat_store.list_media(session_id)
    if not rows:
        return ""
    lines = []
    for i, m in enumerate(rows, 1):
        desc = (m.get("description") or "").strip() or "(no description yet)"
        lines.append(f"{i}. {m['filename']}: {desc}")
    return ("SESSION IMAGES — the user attached these images to this chat. Use "
            "their descriptions to answer questions about them (you may not be "
            "able to see the images directly):\n" + "\n".join(lines))


def image_blocks_for_turn(session_id: int, role: str = "chat") -> list[dict]:
    """Multimodal image blocks for ALL session images, to merge into the user
    turn — ONLY when the model is vision-capable. Empty otherwise."""
    if not vision_enabled(role):
        return []
    from aiforge_core.runtime import chat_store
    blocks: list[dict] = []
    for m in chat_store.list_media(session_id):
        c = vision.attach_image(f"[image: {m['filename']}]", m["path"])
        if isinstance(c, list):
            blocks.extend(c)
    return blocks


__all__ = ["media_dir", "save_image", "vision_enabled", "describe_image",
           "context_block", "image_blocks_for_turn"]
