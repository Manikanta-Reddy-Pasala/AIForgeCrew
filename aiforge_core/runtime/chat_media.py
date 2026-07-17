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


_MAX_FILE_BYTES = 25 * 1024 * 1024     # docs can be bigger than images
_DESC_CAP = 6000                       # extracted-text excerpt cap per doc

_EXT_MIME = {
    ".pdf": "application/pdf",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".csv": "text/csv", ".txt": "text/plain", ".md": "text/markdown",
    ".json": "application/json", ".log": "text/plain", ".yaml": "text/yaml",
    ".yml": "text/yaml", ".py": "text/x-python", ".js": "text/javascript",
    ".ts": "text/plain", ".java": "text/x-java", ".go": "text/x-go",
}
_TEXT_EXTS = {".txt", ".md", ".csv", ".json", ".log", ".yaml", ".yml",
              ".py", ".js", ".ts", ".java", ".go", ".sh", ".sql", ".html",
              ".xml", ".toml", ".ini", ".cfg"}


def _kind_for(mime: str, ext: str) -> str:
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("text/") or ext in _TEXT_EXTS:
        return "text"
    return "document"


def save_file(session_id: int, filename: str, raw: bytes) -> dict:
    """Validate + write an uploaded file (image OR document) to the session's
    media folder. Returns ``{ok, path, mime, filename, kind}`` or
    ``{ok: False, error}``."""
    if len(raw) > _MAX_FILE_BYTES:
        return {"ok": False, "error": "file_too_large",
                "bytes": len(raw), "limit": _MAX_FILE_BYTES}
    name = _safe_name(filename)
    ext = os.path.splitext(name)[1].lower()
    # Prefer magic-byte detection (images); fall back to extension for docs.
    mime = vision._detect_mime(raw) or _EXT_MIME.get(ext) or "application/octet-stream"
    dest = os.path.join(media_dir(session_id), name)
    stem, dext = os.path.splitext(dest)
    n = 1
    while os.path.exists(dest):           # don't clobber same-named uploads
        dest = f"{stem}_{n}{dext}"
        n += 1
    Path(dest).write_bytes(raw)
    return {"ok": True, "path": dest, "mime": mime,
            "filename": os.path.basename(dest), "kind": _kind_for(mime, ext)}


# Back-compat alias (older callers).
save_image = save_file


def extract_text(path: str, mime: str = "") -> str:
    """Pull readable text from a document so the agent can analyse it. Handles
    pdf / xlsx / docx / plain-text. Best-effort, capped, never raises."""
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".pdf" or mime == "application/pdf":
            from pypdf import PdfReader
            r = PdfReader(path)
            return "\n".join((p.extract_text() or "") for p in r.pages[:30])
        if ext == ".xlsx" or "spreadsheet" in mime:
            from openpyxl import load_workbook
            wb = load_workbook(path, read_only=True, data_only=True)
            out: list[str] = []
            for ws in wb.worksheets[:5]:
                out.append(f"# sheet: {ws.title}")
                for i, row in enumerate(ws.iter_rows(values_only=True)):
                    if i >= 200:
                        out.append("… (more rows)")
                        break
                    out.append(", ".join("" if c is None else str(c) for c in row))
            return "\n".join(out)
        if ext == ".docx" or "wordprocessing" in mime:
            import docx
            return "\n".join(p.text for p in docx.Document(path).paragraphs)
        if mime.startswith("text/") or ext in _TEXT_EXTS:
            return Path(path).read_text(encoding="utf-8", errors="ignore")
    except Exception:  # noqa: BLE001
        return ""
    return ""


def describe_upload(path: str, filename: str, mime: str, role: str = "chat") -> str:
    """The text that makes an attachment queryable: a vision caption for an
    image, or an extracted-text excerpt for a document."""
    if mime.startswith("image/"):
        return describe_image(path, role)
    txt = extract_text(path, mime).strip()
    if not txt:
        return ""
    if len(txt) > _DESC_CAP:
        txt = txt[:_DESC_CAP] + "\n… (truncated)"
    return txt


# Vision-capability detection lives in its own module (probe / cache /
# resolve / persist). Re-exported here so existing callers keep using
# ``chat_media.vision_enabled`` / ``reset_vision_cache`` / etc. unchanged.
from .vision_detect import (  # noqa: F401 — re-export for back-compat
    classify_and_store_vision,
    probe_vision_endpoint,
    reset_vision_cache,
    vision_enabled,
    _probe_vision,
    _settings_override,
)


def _vision_role(role: str) -> str | None:
    """A role whose model can actually SEE images: the given role if it's
    vision-capable, else a dedicated vision model — the ``AIFORGE_VISION_ROLE``
    archetype (default 'vision') — when that is configured and vision-capable.
    This lets a text-only chat model (e.g. qwen3-coder) still get image captions
    from a separate VLM (cloud or local) the operator wires up. None when no
    vision model is reachable → the caller returns "" (no caption)."""
    if vision_enabled(role, probe=True):
        return role
    cand = os.environ.get("AIFORGE_VISION_ROLE", "vision")
    if cand == role:
        return None
    try:
        from aiforge_core.llm.router import resolve
        if resolve(cand) is not None and vision_enabled(cand, probe=True):
            return cand
    except Exception:  # noqa: BLE001
        pass
    return None


def describe_image(path: str, role: str = "chat") -> str:
    """Auto-caption an image with a vision model. Uses the role's model when it's
    vision-capable, else a dedicated vision model (see ``_vision_role``).
    Best-effort — returns "" when NO vision model is reachable or the call fails
    (caller falls back to a user-typed caption)."""
    vrole = _vision_role(role)
    if not vrole:
        return ""
    content = vision.attach_image(
        "Describe this image concisely (1-2 sentences) so it can be referenced "
        "later in the conversation. Note any visible text, UI, chart, or code.",
        path)
    if isinstance(content, dict):        # soft-error from attach_image
        return ""
    try:
        from aiforge_core.llm.client import complete
        out = complete(vrole, [{"role": "user", "content": content}],
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


def supported_attachment(mime: str, filename: str) -> bool:
    """Is this attachment one we can analyse — an image, or a document we can
    extract text from (pdf / xlsx / docx / text)?"""
    mime = (mime or "").lower()
    ext = os.path.splitext(filename or "")[1].lower()
    if mime.startswith("image/"):
        return True
    if mime in ("application/pdf",) or "spreadsheet" in mime or \
            "wordprocessing" in mime or mime.startswith("text/"):
        return True
    return ext in {".pdf", ".xlsx", ".docx"} | _TEXT_EXTS


def analyze_attachment(filename: str, raw: bytes, role: str = "doer",
                       mime: str = "") -> dict:
    """Analyse one downloaded attachment (image OR document) for inclusion in a
    tool result. Returns ``{filename, description}`` — a vision caption for an
    image, or extracted text for a document. "" when nothing could be read."""
    # Image → vision caption.
    if vision._detect_mime(raw) is not None or (mime or "").startswith("image/"):
        return {"filename": filename, "description": describe_bytes(raw, role)}
    # Document → extracted text excerpt.
    import tempfile
    ext = os.path.splitext(filename or "")[1].lower() or ".bin"
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as f:
            f.write(raw)
            tmp = f.name
        txt = extract_text(tmp, mime).strip()
        if len(txt) > _DESC_CAP:
            txt = txt[:_DESC_CAP] + "\n… (truncated)"
        return {"filename": filename, "description": txt}
    except Exception:  # noqa: BLE001
        return {"filename": filename, "description": ""}
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except Exception:  # noqa: BLE001
                pass


def context_block(session_id: int) -> str:
    """The "SESSION FILES" block injected into every turn so the model can
    answer questions about attached files (images: caption; documents: their
    extracted text), even when it can't see images directly."""
    from aiforge_core.runtime import chat_store
    rows = chat_store.list_media(session_id)
    if not rows:
        return ""
    lines = []
    for i, m in enumerate(rows, 1):
        desc = (m.get("description") or "").strip() or "(no description/text yet)"
        mime = m.get("mime") or ""
        label = "image" if mime.startswith("image/") else "file"
        lines.append(f"--- {label} {i}: {m['filename']} ---\n{desc}")
    return ("SESSION FILES — the user attached these to this chat (images carry "
            "a description, documents carry their extracted text). Use them to "
            "answer questions about the attachments:\n" + "\n\n".join(lines))


def image_blocks_for_turn(session_id: int, role: str = "chat") -> list[dict]:
    """Multimodal image blocks for ALL session images, to merge into the user
    turn — ONLY when the model is vision-capable. Empty otherwise.

    Images are checked FIRST: a text-only session (the overwhelming majority
    of turns) returns immediately with NO vision probe. The probe makes a
    live LLM call, so probing every turn regardless of attachments made a
    down/slow endpoint block chat setup for the probe timeout × retries —
    only pay that cost when there is actually an image to attach."""
    from aiforge_core.runtime import chat_store
    imgs = [m for m in chat_store.list_media(session_id)
            if (m.get("mime") or "").startswith("image/")]
    if not imgs:
        return []
    if not vision_enabled(role, probe=True):
        return []
    blocks: list[dict] = []
    for m in imgs:
        c = vision.attach_image(f"[image: {m['filename']}]", m["path"])
        if isinstance(c, list):
            blocks.extend(c)
    return blocks


__all__ = ["media_dir", "save_image", "vision_enabled", "describe_image",
           "context_block", "image_blocks_for_turn"]
