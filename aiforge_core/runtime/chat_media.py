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


_MAX_FILE_BYTES = 50 * 1024 * 1024     # docs can be bigger than images
_DESC_CAP = 6000                       # extracted-text excerpt cap per doc

# Document → text extraction (pdf/docx/xlsx text, tables, page segmentation)
# lives in its own module; this file keeps storage + vision + OCR. Re-exported
# so existing callers keep using ``chat_media.extract_text`` / config unchanged.
from .doc_extract import (  # noqa: E402
    _EXT_MIME,
    _TEXT_EXTS,
    _doc_char_budget,
    _int_env,
    _pdf_page_cap,
    document_pages,
    extract_pages,
    extract_text,
    page_count,
)


def _kind_for(mime: str, ext: str) -> str:
    if mime.startswith(_IMAGE):
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


def _docx_images(path: str) -> list[tuple[str, bytes]]:
    """Embedded image parts (name, bytes) from a docx, in package order."""
    import docx
    doc = docx.Document(path)
    out: list[tuple[str, bytes]] = []
    for rel in doc.part.rels.values():
        if "image" in rel.reltype and not rel.is_external:
            part = rel.target_part
            out.append((os.path.basename(part.partname), part.blob))
    return out


_MAX_DOC_IMAGES = 8       # cap vision calls per document


def _pdf_images(path: str) -> list[tuple[str, bytes]]:
    """Embedded raster images (name, bytes) from a PDF via pypdf's per-page
    ``.images`` (Pillow-backed). Bounded to the page cap and _MAX_DOC_IMAGES.
    Best-effort — "" list when pypdf/Pillow can't decode. For a *scanned* PDF
    each page is itself a full-page image, so this also feeds the vision-model
    OCR pass (``_pdf_ocr``) — no OCR engine / new dependency needed."""
    out: list[tuple[str, bytes]] = []
    try:
        from pypdf import PdfReader
        r = PdfReader(path)
        for p in r.pages[:_pdf_page_cap()]:
            for img in p.images:
                out.append((img.name or f"img{len(out)}", img.data))
                if len(out) >= _MAX_DOC_IMAGES:
                    return out
    except Exception:  # noqa: BLE001
        return out
    return out


def _embedded_images(path: str, mime: str) -> list[tuple[str, bytes]]:
    """Embedded images for a doc, dispatched by type (docx / pdf). [] otherwise."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".docx" or "wordprocessing" in (mime or ""):
        return _docx_images(path)
    if ext == ".pdf" or (mime or "") == _APPLICATION_PDF:
        return _pdf_images(path)
    return []


def _embedded_image_captions(path: str, mime: str, role: str) -> str:
    """Caption a document's embedded images via the vision model. Returns a
    block to append to the extracted text, or "" (no images / no vision / all
    failed). Best-effort, never raises."""
    try:
        imgs = _embedded_images(path, mime)
    except Exception:  # noqa: BLE001
        return ""
    if not imgs:
        return ""
    caps: list[str] = []
    for i, (name, blob) in enumerate(imgs[:_MAX_DOC_IMAGES], 1):
        cap = describe_bytes(blob, role).strip()
        if cap:
            caps.append(f"[embedded image {i}: {name}] {cap}")
    if not caps:
        return ""
    return "EMBEDDED IMAGES:\n" + "\n".join(caps)


def _with_doc_images(path: str, mime: str, txt: str, role: str) -> str:
    """Append vision captions of a document's embedded images (docx / pdf) to
    its text excerpt. No-op when no vision model is reachable or no images."""
    caps = _embedded_image_captions(path, mime, role)
    return f"{txt}\n\n{caps}" if caps else txt


def _is_pdf(path: str, mime: str) -> bool:
    return os.path.splitext(path)[1].lower() == ".pdf" \
        or (mime or "") == _APPLICATION_PDF


def _pdf_is_scanned(path: str, mime: str, extracted: str) -> bool:
    """A scanned/image-only PDF yields almost no extractable text. Treat a PDF
    with a near-empty text layer as scanned → OCR it via the vision model."""
    return _is_pdf(path, mime) and len((extracted or "").strip()) < 200


def _page_scan_blobs(page) -> list:
    """The image blobs of a scanned page that has NO extractable text layer.
    Empty when the page has real text (no OCR needed) or no images / errors."""
    try:
        if (page.extract_text() or "").strip():
            return []                      # real text layer → no OCR needed
        return [im.data for im in page.images]
    except Exception:  # noqa: BLE001
        return []


def _pdf_ocr(path: str, role: str) -> str:
    """OCR a scanned PDF using the wired VISION model — no OCR engine, no new
    dependency. Each scanned page is a full-page image (``pypdf`` ``.images``);
    we transcribe pages that have NO extractable text. Bounded by
    ``AIFORGE_PDF_OCR_MAX_PAGES`` (default 30 — vision OCR is one LLM call per
    page) and the shared char budget. Best-effort, never raises."""
    try:
        from pypdf import PdfReader
        r = PdfReader(path)
    except Exception:  # noqa: BLE001
        return ""
    cap = _int_env("AIFORGE_PDF_OCR_MAX_PAGES", 30)
    budget = _doc_char_budget()
    out: list[str] = []
    used = done = 0
    for i, page in enumerate(r.pages[:_pdf_page_cap()]):
        blobs = _page_scan_blobs(page)
        if not blobs:
            continue
        if done >= cap:
            out.append(f"… (OCR stopped at {cap} pages)")
            break
        page_img = max(blobs, key=len)        # the page scan = largest image
        txt = describe_bytes(page_img, role, prompt=_OCR_PROMPT,
                             max_tokens=1500).strip()
        done += 1
        if not txt:
            continue
        block = f"[OCR page {i + 1}]\n{txt}"
        out.append(block)
        used += len(block)
        if used >= budget:
            out.append(f"… (OCR char budget {budget} reached)")
            break
    return "\n\n".join(out)


def _summarize_or_excerpt(full: str, role: str) -> str:
    """Auto-select by document size — the strategy escalates with length:

      • small  (≤ _DESC_CAP)         → raw text, verbatim (no LLM cost)
      • large  (> _DESC_CAP)         → map-reduce SUMMARY (doc_summarize picks
                                        single-shot vs windowed internally, so a
                                        400-page doc folds without blowing ctx)

    Falls back to a plain truncated excerpt when summarisation is disabled or
    the model is unreachable."""
    full = (full or "").strip()
    if len(full) <= _DESC_CAP:
        return full
    from aiforge_core.runtime import doc_summarize
    summary = doc_summarize.summarize_text(full, role).strip()
    if summary and not full.startswith(summary):
        return (f"SUMMARY (auto-generated from {len(full)} chars of extracted "
                f"text):\n{summary}")
    return full[:_DESC_CAP] + "\n… (truncated)"


def describe_upload(path: str, _filename: str, mime: str, role: str = "chat") -> str:
    """The text that makes an attachment queryable: a vision caption for an
    image, or — for a document — a size-selected excerpt/summary (small doc:
    raw text; large doc: map-reduce summary). docx image captions appended when
    a vision model is reachable."""
    if mime.startswith(_IMAGE):
        return describe_image(path, role)
    full = extract_text(path, mime).strip()
    scanned = _pdf_is_scanned(path, mime, full)
    if scanned:                               # image-only PDF → vision-model OCR
        ocr = _pdf_ocr(path, role)
        if ocr:
            full = ocr
    body = _summarize_or_excerpt(full, role)
    if not scanned:                           # figure captions (skip for scans)
        body = _with_doc_images(path, mime, body, role)
    return body.strip()


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

_APPLICATION_PDF = 'application/pdf'
_IMAGE = 'image/'


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


_CAPTION_PROMPT = (
    "Describe this image concisely (1-2 sentences) so it can be referenced "
    "later in the conversation. Note any visible text, UI, chart, or code.")

_OCR_PROMPT = (
    "Transcribe ALL text in this image exactly, preserving reading order and "
    "line breaks. Output only the transcribed text — no commentary, no "
    "description. If there is no readable text, output nothing.")


def describe_image(path: str, role: str = "chat", *,
                   prompt: str = _CAPTION_PROMPT, max_tokens: int = 200) -> str:
    """Run a vision model over an image. Default prompt = a short caption; pass
    ``prompt=_OCR_PROMPT`` (and a larger ``max_tokens``) to transcribe a scanned
    page instead. Uses the role's model when vision-capable, else a dedicated
    vision model (see ``_vision_role``). Best-effort — "" when no vision model
    is reachable or the call fails."""
    vrole = _vision_role(role)
    if not vrole:
        return ""
    content = vision.attach_image(prompt, path)
    if isinstance(content, dict):        # soft-error from attach_image
        return ""
    try:
        from aiforge_core.llm.client import complete
        out = complete(vrole, [{"role": "user", "content": content}],
                       max_tokens=max_tokens)
        return (out or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def describe_bytes(raw: bytes, role: str = "doer", *,
                   prompt: str = _CAPTION_PROMPT, max_tokens: int = 200) -> str:
    """Vision model over raw image bytes (e.g. a Jira/Confluence attachment or a
    PDF page scan). Default = caption; pass ``prompt=_OCR_PROMPT`` to OCR. "" when
    not an image, vision is off, or the call fails."""
    import tempfile
    if vision._detect_mime(raw) is None:
        return ""
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".img", delete=False) as f:
            f.write(raw)
            tmp = f.name
        return describe_image(tmp, role, prompt=prompt, max_tokens=max_tokens)
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
    if mime.startswith(_IMAGE):
        return True
    if mime in (_APPLICATION_PDF,) or "spreadsheet" in mime or \
            "wordprocessing" in mime or mime.startswith("text/"):
        return True
    return ext in {".pdf", ".xlsx", ".docx"} | _TEXT_EXTS


def _extract_document_text(tmp: str, mime: str, role: str) -> str:
    """The text of a document temp file — a map-reduce summary / excerpt for a
    normal doc, or vision-model OCR for a scanned (image-only) PDF."""
    full = extract_text(tmp, mime).strip()
    scanned = _pdf_is_scanned(tmp, mime, full)
    if scanned:                           # image-only PDF → vision-model OCR
        ocr = _pdf_ocr(tmp, role)
        if ocr:
            full = ocr
    txt = _summarize_or_excerpt(full, role)
    if not scanned:
        txt = _with_doc_images(tmp, mime, txt, role)
    return txt.strip()


def analyze_attachment(filename: str, raw: bytes, role: str = "doer",
                       mime: str = "") -> dict:
    """Analyse one downloaded attachment (image OR document) for inclusion in a
    tool result. Returns ``{filename, description}`` — a vision caption for an
    image, or extracted text for a document. "" when nothing could be read."""
    if vision._detect_mime(raw) is not None or (mime or "").startswith(_IMAGE):
        return {"filename": filename, "description": describe_bytes(raw, role)}
    import tempfile
    ext = os.path.splitext(filename or "")[1].lower() or ".bin"
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as f:
            f.write(raw)
            tmp = f.name
        return {"filename": filename,
                "description": _extract_document_text(tmp, mime, role)}
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
        label = "image" if mime.startswith(_IMAGE) else "file"
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
            if (m.get("mime") or "").startswith(_IMAGE)]
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
