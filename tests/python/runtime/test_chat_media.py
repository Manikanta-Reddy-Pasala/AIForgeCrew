"""Chat attachments: storage, vision captions, PDF OCR, and the context block.

The interesting decisions here are about cost and reach. A document escalates
by size — small ones go in verbatim, large ones through a map-reduce summary —
because pasting a 400-page PDF into the window is not an option. A scanned PDF
has no text layer, so it is OCR'd through the VISION model rather than a new
OCR dependency, bounded by a page cap and a char budget since each page is an
LLM call. And a text-only chat model can still get captions from a separate
VLM the operator wires up as the ``vision`` role.

One performance rule is pinned explicitly: the vision probe is a live LLM
call, so a turn with NO images must never pay for it.
"""
from __future__ import annotations

import os

import pytest

from aiforge_core.runtime import chat_media as cm


@pytest.fixture(autouse=True)
def workspace(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CHAT_WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.delenv("AIFORGE_VISION_ROLE", raising=False)
    return tmp_path


# ─── storage ───────────────────────────────────────────────────────────


def test_media_lives_under_the_session_workspace(workspace):
    d = cm.media_dir(7)
    assert d.endswith(os.path.join("session-7", ".aiforge", "media"))
    assert os.path.isdir(d)


def test_the_config_root_is_honoured(monkeypatch, tmp_path):
    monkeypatch.delenv("AIFORGE_CHAT_WORKSPACE_ROOT", raising=False)
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    assert str(tmp_path / "cfg" / "chat-workspaces") in cm.media_dir(1)


@pytest.mark.parametrize("raw,safe", [
    ("photo.png", "photo.png"),
    ("../../etc/passwd", "passwd"),
    ("my file (1).png", "myfile1.png"),
    ("", "image"),
    ("!!!", "image"),
])
def test_upload_names_are_sanitised(raw, safe):
    assert cm._safe_name(raw) == safe


def test_an_image_is_stored_with_its_detected_mime(workspace, monkeypatch):
    monkeypatch.setattr(cm.vision, "_detect_mime", lambda raw: "image/png")
    out = cm.save_file(3, "shot.png", b"\x89PNG")
    assert out["ok"] is True
    assert out["kind"] == "image"
    assert out["mime"] == "image/png"
    assert open(out["path"], "rb").read() == b"\x89PNG"


def test_a_document_falls_back_to_the_extension_mime(workspace, monkeypatch):
    monkeypatch.setattr(cm.vision, "_detect_mime", lambda raw: None)
    out = cm.save_file(3, "spec.pdf", b"%PDF-")
    assert out["kind"] == "document"
    assert out["mime"] == "application/pdf"


def test_an_unknown_type_is_stored_as_a_document(workspace, monkeypatch):
    monkeypatch.setattr(cm.vision, "_detect_mime", lambda raw: None)
    assert cm.save_file(3, "thing.bin", b"x")["mime"] == "application/octet-stream"


def test_a_text_file_is_recognised(workspace, monkeypatch):
    monkeypatch.setattr(cm.vision, "_detect_mime", lambda raw: None)
    assert cm.save_file(3, "notes.txt", b"hi")["kind"] == "text"


def test_a_same_named_upload_never_clobbers_the_first(workspace, monkeypatch):
    monkeypatch.setattr(cm.vision, "_detect_mime", lambda raw: "image/png")
    first = cm.save_file(3, "shot.png", b"one")
    second = cm.save_file(3, "shot.png", b"two")
    assert first["filename"] == "shot.png"
    assert second["filename"] == "shot_1.png"
    assert open(first["path"], "rb").read() == b"one"


def test_an_oversized_file_is_refused(workspace):
    out = cm.save_file(3, "huge.bin", b"x" * (cm._MAX_FILE_BYTES + 1))
    assert out["ok"] is False
    assert out["error"] == "file_too_large"
    assert out["limit"] == cm._MAX_FILE_BYTES


def test_the_legacy_alias_still_saves(workspace, monkeypatch):
    monkeypatch.setattr(cm.vision, "_detect_mime", lambda raw: "image/png")
    assert cm.save_image(3, "a.png", b"x")["ok"] is True


# ─── which vision model answers ────────────────────────────────────────


def test_a_vision_capable_role_is_used_directly(monkeypatch):
    monkeypatch.setattr(cm, "vision_enabled", lambda role, probe=False: role == "chat")
    assert cm._vision_role("chat") == "chat"


def test_a_text_only_role_borrows_the_dedicated_vision_model(monkeypatch):
    """A qwen3-coder chat model still gets captions from a separate VLM."""
    monkeypatch.setattr(cm, "vision_enabled",
                        lambda role, probe=False: role == "vision")
    import aiforge_core.llm.router as router
    monkeypatch.setattr(router, "resolve", lambda role: object())
    assert cm._vision_role("chat") == "vision"


def test_no_vision_anywhere_means_no_caption(monkeypatch):
    monkeypatch.setattr(cm, "vision_enabled", lambda role, probe=False: False)
    import aiforge_core.llm.router as router
    monkeypatch.setattr(router, "resolve", lambda role: object())
    assert cm._vision_role("chat") is None


def test_the_vision_role_does_not_borrow_from_itself(monkeypatch):
    monkeypatch.setattr(cm, "vision_enabled", lambda role, probe=False: False)
    assert cm._vision_role("vision") is None


def test_an_unroutable_vision_role_is_not_used(monkeypatch):
    monkeypatch.setattr(cm, "vision_enabled", lambda role, probe=False: role == "vision")
    import aiforge_core.llm.router as router
    monkeypatch.setattr(router, "resolve", lambda role: None)
    assert cm._vision_role("chat") is None


def test_a_broken_router_is_not_fatal(monkeypatch):
    monkeypatch.setattr(cm, "vision_enabled", lambda role, probe=False: False)
    import aiforge_core.llm.router as router
    monkeypatch.setattr(router, "resolve",
                        lambda role: (_ for _ in ()).throw(RuntimeError("no config")))
    assert cm._vision_role("chat") is None


# ─── captioning ────────────────────────────────────────────────────────


@pytest.fixture
def vision_model(monkeypatch):
    seen: dict = {"reply": "a screenshot of a chart"}
    monkeypatch.setattr(cm, "_vision_role", lambda role: "vision")
    monkeypatch.setattr(cm.vision, "attach_image",
                        lambda prompt, path: [{"type": "text", "text": prompt}])
    import aiforge_core.llm.client as client

    def _complete(role, messages, max_tokens=None):
        seen.update(role=role, messages=messages, max_tokens=max_tokens)
        return seen["reply"]
    monkeypatch.setattr(client, "complete", _complete)
    return seen


def test_an_image_is_captioned(vision_model, tmp_path):
    assert cm.describe_image(str(tmp_path / "a.png")) == "a screenshot of a chart"
    assert vision_model["role"] == "vision"
    assert vision_model["max_tokens"] == 200


def test_the_ocr_prompt_asks_for_a_transcription(vision_model, tmp_path):
    cm.describe_image(str(tmp_path / "a.png"), prompt=cm._OCR_PROMPT,
                      max_tokens=1500)
    assert "Transcribe ALL text" in vision_model["messages"][0]["content"][0]["text"]
    assert vision_model["max_tokens"] == 1500


def test_no_vision_model_means_no_caption(monkeypatch, tmp_path):
    monkeypatch.setattr(cm, "_vision_role", lambda role: None)
    assert cm.describe_image(str(tmp_path / "a.png")) == ""


def test_an_unreadable_image_produces_no_caption(vision_model, monkeypatch, tmp_path):
    monkeypatch.setattr(cm.vision, "attach_image",
                        lambda prompt, path: {"error": "too big"})
    assert cm.describe_image(str(tmp_path / "a.png")) == ""


def test_a_failing_vision_call_produces_no_caption(vision_model, monkeypatch, tmp_path):
    import aiforge_core.llm.client as client
    monkeypatch.setattr(client, "complete",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    assert cm.describe_image(str(tmp_path / "a.png")) == ""


def test_raw_bytes_are_captioned_through_a_temp_file(vision_model, monkeypatch):
    monkeypatch.setattr(cm.vision, "_detect_mime", lambda raw: "image/png")
    seen: dict = {}

    def _describe(path, role, prompt=None, max_tokens=None):
        seen["exists"] = os.path.exists(path)
        seen["path"] = path
        return "caption"
    monkeypatch.setattr(cm, "describe_image", _describe)
    assert cm.describe_bytes(b"\x89PNG") == "caption"
    assert seen["exists"] is True
    assert not os.path.exists(seen["path"])          # cleaned up afterwards


def test_non_image_bytes_are_not_captioned(monkeypatch):
    monkeypatch.setattr(cm.vision, "_detect_mime", lambda raw: None)
    assert cm.describe_bytes(b"%PDF-") == ""


def test_a_crash_while_captioning_bytes_is_swallowed(monkeypatch):
    monkeypatch.setattr(cm.vision, "_detect_mime", lambda raw: "image/png")
    monkeypatch.setattr(cm, "describe_image",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert cm.describe_bytes(b"x") == ""


# ─── scanned PDFs ──────────────────────────────────────────────────────


@pytest.mark.parametrize("path,mime,is_pdf", [
    ("a.pdf", "", True), ("a.bin", "application/pdf", True),
    ("a.docx", "application/msword", False),
])
def test_pdf_detection(path, mime, is_pdf):
    assert cm._is_pdf(path, mime) is is_pdf


def test_a_pdf_with_almost_no_text_is_treated_as_scanned():
    assert cm._pdf_is_scanned("a.pdf", "", "  ") is True
    assert cm._pdf_is_scanned("a.pdf", "", "x" * 500) is False
    assert cm._pdf_is_scanned("a.docx", "", "") is False


class _Page:
    def __init__(self, text="", images=()):
        self._text = text
        self.images = [type("I", (), {"data": b, "name": f"i{i}"})()
                       for i, b in enumerate(images)]

    def extract_text(self):
        return self._text


def test_a_page_with_a_text_layer_is_not_ocrd():
    assert cm._page_scan_blobs(_Page(text="real text", images=(b"x",))) == []


def test_a_scanned_page_yields_its_blobs():
    assert cm._page_scan_blobs(_Page(images=(b"x",))) == [b"x"]


def test_an_unreadable_page_yields_nothing():
    class _Bad:
        def extract_text(self):
            raise RuntimeError("corrupt")
    assert cm._page_scan_blobs(_Bad()) == []


@pytest.fixture
def pdf(monkeypatch):
    import pypdf
    state = {"pages": [_Page(images=(b"small", b"biggest-page-scan"))]}

    class _R:
        def __init__(self, path):
            self.pages = state["pages"]
    monkeypatch.setattr(pypdf, "PdfReader", _R)
    monkeypatch.setattr(cm, "describe_bytes",
                        lambda blob, role, prompt=None, max_tokens=None:
                        f"text of {blob.decode()}")
    monkeypatch.delenv("AIFORGE_PDF_OCR_MAX_PAGES", raising=False)
    return state


def test_the_largest_image_on_a_page_is_the_scan(pdf):
    out = cm._pdf_ocr("/doc.pdf", "chat")
    assert "[OCR page 1]" in out
    assert "biggest-page-scan" in out


def test_pages_with_text_are_skipped(pdf):
    pdf["pages"] = [_Page(text="real"), _Page(images=(b"scan",))]
    assert cm._pdf_ocr("/doc.pdf", "chat").count("[OCR page") == 1


def test_ocr_stops_at_the_page_cap(pdf, monkeypatch):
    """Each page is one LLM call."""
    monkeypatch.setenv("AIFORGE_PDF_OCR_MAX_PAGES", "2")
    pdf["pages"] = [_Page(images=(b"s",)) for _ in range(5)]
    out = cm._pdf_ocr("/doc.pdf", "chat")
    assert out.count("[OCR page") == 2
    assert "OCR stopped at 2 pages" in out


def test_ocr_stops_at_the_char_budget(pdf, monkeypatch):
    monkeypatch.setenv("AIFORGE_DOC_MAX_CHARS", "40")
    pdf["pages"] = [_Page(images=(b"a" * 100,)) for _ in range(4)]
    assert "char budget" in cm._pdf_ocr("/doc.pdf", "chat")


def test_an_empty_transcription_is_dropped(pdf, monkeypatch):
    monkeypatch.setattr(cm, "describe_bytes", lambda *a, **k: "   ")
    assert cm._pdf_ocr("/doc.pdf", "chat") == ""


def test_an_unreadable_pdf_ocrs_to_nothing(monkeypatch):
    import pypdf
    monkeypatch.setattr(pypdf, "PdfReader",
                        lambda path: (_ for _ in ()).throw(RuntimeError("corrupt")))
    assert cm._pdf_ocr("/doc.pdf", "chat") == ""


# ─── embedded images ───────────────────────────────────────────────────


def test_pdf_images_are_bounded(monkeypatch):
    import pypdf

    class _R:
        pages = [_Page(images=tuple(b"x" for _ in range(20)))]
    monkeypatch.setattr(pypdf, "PdfReader", lambda path: _R())
    assert len(cm._pdf_images("/doc.pdf")) == cm._MAX_DOC_IMAGES


def test_an_unreadable_pdf_has_no_images(monkeypatch):
    import pypdf
    monkeypatch.setattr(pypdf, "PdfReader",
                        lambda path: (_ for _ in ()).throw(RuntimeError("bad")))
    assert cm._pdf_images("/doc.pdf") == []


@pytest.mark.parametrize("path,mime,expect", [
    ("a.pdf", "", "pdf"),
    ("a.bin", "application/pdf", "pdf"),
    ("a.docx", "", "docx"),
    ("a.bin", "application/vnd.openxmlformats-officedocument.wordprocessingml", "docx"),
    ("a.txt", "text/plain", None),
])
def test_embedded_images_are_dispatched_by_type(monkeypatch, path, mime, expect):
    monkeypatch.setattr(cm, "_pdf_images", lambda p: [("pdf", b"x")])
    monkeypatch.setattr(cm, "_docx_images", lambda p: [("docx", b"x")])
    out = cm._embedded_images(path, mime)
    assert (out[0][0] if out else None) == expect


def test_captions_are_numbered_and_named(monkeypatch):
    monkeypatch.setattr(cm, "_embedded_images",
                        lambda path, mime: [("chart.png", b"a"), ("logo.png", b"b")])
    monkeypatch.setattr(cm, "describe_bytes", lambda blob, role: "a chart")
    out = cm._embedded_image_captions("/a.pdf", "", "chat")
    assert out.startswith("EMBEDDED IMAGES:")
    assert "[embedded image 1: chart.png] a chart" in out


def test_no_images_means_no_block(monkeypatch):
    monkeypatch.setattr(cm, "_embedded_images", lambda path, mime: [])
    assert cm._embedded_image_captions("/a.pdf", "", "chat") == ""


def test_uncaptionable_images_produce_no_block(monkeypatch):
    monkeypatch.setattr(cm, "_embedded_images", lambda path, mime: [("a", b"x")])
    monkeypatch.setattr(cm, "describe_bytes", lambda blob, role: "")
    assert cm._embedded_image_captions("/a.pdf", "", "chat") == ""


def test_a_broken_extractor_produces_no_block(monkeypatch):
    monkeypatch.setattr(cm, "_embedded_images",
                        lambda path, mime: (_ for _ in ()).throw(RuntimeError("bad")))
    assert cm._embedded_image_captions("/a.pdf", "", "chat") == ""


def test_captions_are_appended_to_the_text(monkeypatch):
    monkeypatch.setattr(cm, "_embedded_image_captions",
                        lambda path, mime, role: "EMBEDDED IMAGES:\n[1] x")
    assert cm._with_doc_images("/a.pdf", "", "body", "chat").startswith("body\n\n")


def test_no_captions_leaves_the_text_alone(monkeypatch):
    monkeypatch.setattr(cm, "_embedded_image_captions", lambda *a: "")
    assert cm._with_doc_images("/a.pdf", "", "body", "chat") == "body"


# ─── size-selected description ─────────────────────────────────────────


def test_a_small_document_goes_in_verbatim(monkeypatch):
    from aiforge_core.runtime import doc_summarize
    monkeypatch.setattr(doc_summarize, "summarize_text",
                        lambda text, role: pytest.fail("summarised a short doc"))
    assert cm._summarize_or_excerpt("  short text  ", "chat") == "short text"


def test_a_large_document_is_summarised(monkeypatch):
    from aiforge_core.runtime import doc_summarize
    monkeypatch.setattr(doc_summarize, "summarize_text",
                        lambda text, role: "the gist")
    out = cm._summarize_or_excerpt("x" * 9000, "chat")
    assert out.startswith("SUMMARY (auto-generated from 9000 chars")
    assert "the gist" in out


def test_an_unreachable_summariser_falls_back_to_an_excerpt(monkeypatch):
    from aiforge_core.runtime import doc_summarize
    monkeypatch.setattr(doc_summarize, "summarize_text", lambda text, role: "")
    out = cm._summarize_or_excerpt("y" * 9000, "chat")
    assert out.endswith("… (truncated)")
    assert len(out) < 9000


def test_an_image_upload_is_described_by_caption(monkeypatch):
    monkeypatch.setattr(cm, "describe_image", lambda path, role: "a chart")
    assert cm.describe_upload("/a.png", "a.png", "image/png") == "a chart"


def test_a_document_upload_is_described_by_its_text(monkeypatch):
    monkeypatch.setattr(cm, "extract_text", lambda path, mime: "the contents")
    monkeypatch.setattr(cm, "_with_doc_images",
                        lambda path, mime, txt, role: txt + "\n\ncaptions")
    out = cm.describe_upload("/a.docx", "a.docx", "application/msword")
    assert out == "the contents\n\ncaptions"


def test_a_scanned_pdf_is_ocrd_and_not_image_captioned(monkeypatch):
    monkeypatch.setattr(cm, "extract_text", lambda path, mime: "")
    monkeypatch.setattr(cm, "_pdf_ocr", lambda path, role: "OCR text")
    monkeypatch.setattr(cm, "_with_doc_images",
                        lambda *a: pytest.fail("captioned a page scan"))
    assert cm.describe_upload("/a.pdf", "a.pdf", "application/pdf") == "OCR text"


def test_a_scanned_pdf_whose_ocr_fails_falls_back_to_the_text_layer(monkeypatch):
    monkeypatch.setattr(cm, "extract_text", lambda path, mime: "scraps")
    monkeypatch.setattr(cm, "_pdf_ocr", lambda path, role: "")
    assert cm.describe_upload("/a.pdf", "a.pdf", "application/pdf") == "scraps"


# ─── attachment analysis ───────────────────────────────────────────────


@pytest.mark.parametrize("mime,filename,ok", [
    ("image/png", "a.png", True),
    ("application/pdf", "a.pdf", True),
    ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "a.xlsx", True),
    ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "a.docx", True),
    ("text/plain", "a.txt", True),
    ("", "a.pdf", True),
    ("", "a.docx", True),
    ("application/zip", "a.zip", False),
    ("", "", False),
])
def test_which_attachments_can_be_analysed(mime, filename, ok):
    assert cm.supported_attachment(mime, filename) is ok


def test_an_image_attachment_is_captioned(monkeypatch):
    monkeypatch.setattr(cm.vision, "_detect_mime", lambda raw: "image/png")
    monkeypatch.setattr(cm, "describe_bytes", lambda raw, role: "a chart")
    assert cm.analyze_attachment("a.png", b"x") == {"filename": "a.png",
                                                    "description": "a chart"}


def test_a_document_attachment_is_extracted(monkeypatch):
    monkeypatch.setattr(cm.vision, "_detect_mime", lambda raw: None)
    seen: dict = {}

    def _extract(tmp, mime, role):
        seen["suffix"] = os.path.splitext(tmp)[1]
        return "the contents"
    monkeypatch.setattr(cm, "_extract_document_text", _extract)
    out = cm.analyze_attachment("spec.pdf", b"%PDF-", mime="application/pdf")
    assert out["description"] == "the contents"
    assert seen["suffix"] == ".pdf"        # the extractor needs the real type


def test_an_extension_less_attachment_still_gets_a_temp_file(monkeypatch):
    monkeypatch.setattr(cm.vision, "_detect_mime", lambda raw: None)
    seen: dict = {}
    monkeypatch.setattr(cm, "_extract_document_text",
                        lambda tmp, mime, role: seen.setdefault("suffix",
                                                                os.path.splitext(tmp)[1]) or "")
    cm.analyze_attachment("noext", b"x")
    assert seen["suffix"] == ".bin"


def test_an_unreadable_attachment_describes_to_nothing(monkeypatch):
    monkeypatch.setattr(cm.vision, "_detect_mime", lambda raw: None)
    monkeypatch.setattr(cm, "_extract_document_text",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("corrupt")))
    assert cm.analyze_attachment("a.pdf", b"x")["description"] == ""


# ─── the per-turn context ──────────────────────────────────────────────


@pytest.fixture
def media_rows(monkeypatch):
    from aiforge_core.runtime import chat_store
    rows: list = []
    monkeypatch.setattr(chat_store, "list_media", lambda sid: rows)
    return rows


def test_no_attachments_means_no_block(media_rows):
    assert cm.context_block(1) == ""


def test_images_and_files_are_labelled(media_rows):
    media_rows.extend([
        {"filename": "shot.png", "mime": "image/png", "description": "a chart"},
        {"filename": "spec.pdf", "mime": "application/pdf", "description": "text"},
    ])
    out = cm.context_block(1)
    assert "--- image 1: shot.png ---\na chart" in out
    assert "--- file 2: spec.pdf ---\ntext" in out
    assert out.startswith("SESSION FILES")


def test_an_undescribed_attachment_still_appears(media_rows):
    media_rows.append({"filename": "a.png", "mime": "image/png", "description": ""})
    assert "(no description/text yet)" in cm.context_block(1)


def test_a_text_only_turn_never_pays_for_the_vision_probe(media_rows, monkeypatch):
    """The probe is a live LLM call — a down endpoint would block chat setup
    for the probe timeout on every turn."""
    media_rows.append({"filename": "spec.pdf", "mime": "application/pdf",
                       "path": "/x"})
    monkeypatch.setattr(cm, "vision_enabled",
                        lambda role, probe=False: pytest.fail("probed with no images"))
    assert cm.image_blocks_for_turn(1) == []


def test_images_are_attached_when_the_model_can_see(media_rows, monkeypatch):
    media_rows.append({"filename": "a.png", "mime": "image/png", "path": "/a.png"})
    monkeypatch.setattr(cm, "vision_enabled", lambda role, probe=False: True)
    monkeypatch.setattr(cm.vision, "attach_image",
                        lambda text, path: [{"type": "image", "path": path}])
    assert cm.image_blocks_for_turn(1) == [{"type": "image", "path": "/a.png"}]


def test_a_text_only_model_gets_no_image_blocks(media_rows, monkeypatch):
    media_rows.append({"filename": "a.png", "mime": "image/png", "path": "/a.png"})
    monkeypatch.setattr(cm, "vision_enabled", lambda role, probe=False: False)
    assert cm.image_blocks_for_turn(1) == []


def test_an_unattachable_image_is_skipped(media_rows, monkeypatch):
    media_rows.append({"filename": "a.png", "mime": "image/png", "path": "/a.png"})
    monkeypatch.setattr(cm, "vision_enabled", lambda role, probe=False: True)
    monkeypatch.setattr(cm.vision, "attach_image",
                        lambda text, path: {"error": "too big"})
    assert cm.image_blocks_for_turn(1) == []
