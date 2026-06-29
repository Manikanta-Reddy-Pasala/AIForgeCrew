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


# 1x1 transparent PNG — the smallest valid image to probe with.
_PROBE_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")

# model id -> probed vision capability. One live probe per model, then cached.
_VISION_CACHE: dict[str, bool] = {}


def _settings_override() -> bool:
    try:
        from aiforge_core.config import runtime_settings
        return int(runtime_settings.get("vision_capable")) > 0
    except Exception:  # noqa: BLE001
        return False


def _probe_vision(model: str, role: str) -> bool:
    """Ask the OpenAI-compatible endpoint itself whether it accepts image input
    — NO hardcoded model list. Sends one tiny multimodal request; a server that
    can't do vision rejects the image content (4xx) → False, one that accepts
    it → True. Cached per model so it costs one probe. Transport errors are
    inconclusive (not cached)."""
    if model in _VISION_CACHE:
        return _VISION_CACHE[model]
    import base64 as _b64
    try:
        from aiforge_core.llm import client
        content = [
            {"type": "text", "text": "Reply with the single word: ok"},
            {"type": "image_url",
             "image_url": {"url": "data:image/png;base64," + _PROBE_PNG}},
        ]
        # A non-vision server raises (4xx invalid content) → caught below.
        client.complete(role, [{"role": "user", "content": content}],
                        max_tokens=1, timeout_s=20)
        _VISION_CACHE[model] = True
        return True
    except Exception as exc:  # noqa: BLE001
        # Only a content/modality rejection is a definitive "no"; a transport
        # blip shouldn't permanently mark the model non-vision.
        msg = str(exc).lower()
        definitive = any(t in msg for t in (
            "image", "modal", "content", "vision", "unsupported",
            "400", "422", "invalid"))
        if definitive:
            _VISION_CACHE[model] = False
        return False


def reset_vision_cache() -> None:
    _VISION_CACHE.clear()


def vision_enabled(role: str = "chat", *, probe: bool = False) -> bool:
    """True when the session's model can see images. The user's manual setting
    wins; otherwise it's probed from the OpenAI-compatible endpoint itself (no
    hardcoded allowlist). ``probe=False`` (default) only consults the settings
    override + a cached prior probe — fast, used on session-load. ``probe=True``
    runs the one-time live probe — used on the upload path where a brief delay
    is expected."""
    if _settings_override():
        return True
    try:
        from aiforge_core.llm.router import resolve
        model = resolve(role).model or ""
    except Exception:  # noqa: BLE001
        return False
    if not model:
        return False
    if probe:
        return _probe_vision(model, role)
    return _VISION_CACHE.get(model, False)


def describe_image(path: str, role: str = "chat") -> str:
    """Auto-caption an image with the vision model. Best-effort — returns ""
    when vision is unavailable or the call fails (caller falls back to a
    user-typed caption)."""
    if not vision_enabled(role, probe=True):
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


def describe_bytes(raw: bytes, role: str = "doer") -> str:
    """Auto-caption raw image bytes (e.g. a Jira/Confluence attachment) via the
    vision model. "" when not an image, vision is off, or the call fails."""
    import tempfile
    if vision._detect_mime(raw) is None:
        return ""
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".img", delete=False) as f:
            f.write(raw)
            tmp = f.name
        return describe_image(tmp, role)
    except Exception:  # noqa: BLE001
        return ""
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except Exception:  # noqa: BLE001
                pass


def analyze_attachment(filename: str, raw: bytes, role: str = "doer") -> dict:
    """Describe one downloaded image attachment for inclusion in a tool result.
    Returns ``{filename, description}``; description is "" when vision is off
    (the agent still sees the filename, and can be told to enable vision)."""
    return {"filename": filename, "description": describe_bytes(raw, role)}


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
    if not vision_enabled(role, probe=True):
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
