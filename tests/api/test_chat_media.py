"""Chat image attachments: upload → store → list → describe → delete, plus the
description reaching the agent context and NOT polluting the session summary."""
import importlib

import pytest
from fastapi.testclient import TestClient

# Smallest valid PNG (1x1) — magic bytes pass vision._detect_mime.
_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082")


@pytest.fixture
def app_client(monkeypatch, tmp_path):
    monkeypatch.delenv("AIFORGE_PG_URL", raising=False)
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_CHAT_DB_PATH", str(tmp_path / "chat.db"))
    monkeypatch.setenv("AIFORGE_CHAT_WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("AIFORGE_DB_PATH", str(tmp_path / "api.db"))
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "mem.db"))
    for k in ("AIFORGE_MEMORY_BACKEND", "AIFORGE_NEO4J_URI", "NEO4J_URI"):
        monkeypatch.delenv(k, raising=False)
    import aiforge_core.config.env as envmod
    importlib.reload(envmod)
    import aiforge_core.tickets.backend_factory as bf
    importlib.reload(bf)
    bf.reset_backend_for_tests()
    import aiforge_core.tickets.store as store
    importlib.reload(store)
    import aiforge_core.api.api as api
    importlib.reload(api)
    return TestClient(api.app), api


def _new_session(client) -> int:
    return client.post("/api/chat/sessions", json={"title": "t"}).json()["id"]


def test_upload_list_describe_delete(app_client):
    client, _ = app_client
    sid = _new_session(client)

    # Upload (no vision model in test → no auto-description; manual caption).
    r = client.post(f"/api/chat/sessions/{sid}/media",
                    files={"file": ("shot.png", _PNG, "image/png")})
    assert r.status_code == 201, r.text
    media = r.json()
    assert media["filename"].endswith(".png") and media["mime"] == "image/png"
    assert media["auto_described"] is False
    mid = media["id"]

    # List + vision flag present.
    lst = client.get(f"/api/chat/sessions/{sid}/media").json()
    assert len(lst["media"]) == 1 and lst["vision"] is False

    # Add a caption → it becomes the queryable description.
    pr = client.patch(f"/api/chat/media/{mid}",
                      json={"description": "a login screen with a red error"})
    assert pr.json()["description"] == "a login screen with a red error"

    # Raw bytes are served.
    raw = client.get(f"/api/chat/media/{mid}/raw")
    assert raw.status_code == 200 and raw.content[:4] == b"\x89PNG"

    # Delete removes the row + file.
    assert client.delete(f"/api/chat/media/{mid}").status_code == 204
    assert client.get(f"/api/chat/sessions/{sid}/media").json()["media"] == []


def test_context_block_carries_description(app_client):
    client, _ = app_client
    sid = _new_session(client)
    mid = client.post(f"/api/chat/sessions/{sid}/media",
                      files={"file": ("c.png", _PNG, "image/png")}).json()["id"]
    client.patch(f"/api/chat/media/{mid}",
                 json={"description": "UNIQUEMARKER bar chart"})
    from aiforge_core.runtime import chat_media
    block = chat_media.context_block(sid)
    assert "SESSION FILES" in block and "UNIQUEMARKER bar chart" in block


def test_accepts_text_document_and_extracts_text(app_client):
    """Non-image files (pdf/xls/docx/text) are now accepted; a text file's
    content becomes its description (extracted text), queryable in-session."""
    client, _ = app_client
    sid = _new_session(client)
    r = client.post(f"/api/chat/sessions/{sid}/media",
                    files={"file": ("notes.txt",
                                    b"deploy steps: pull, build, restart api",
                                    "text/plain")})
    assert r.status_code == 201, r.text
    row = r.json()
    assert row["kind"] == "text"
    assert "deploy steps" in row["description"]      # text extracted
    from aiforge_core.runtime import chat_media
    assert "notes.txt" in chat_media.context_block(sid)


def test_reject_oversize_file(app_client, monkeypatch):
    from aiforge_core.runtime import chat_media
    monkeypatch.setattr(chat_media, "_MAX_FILE_BYTES", 8)
    client, _ = app_client
    sid = _new_session(client)
    r = client.post(f"/api/chat/sessions/{sid}/media",
                    files={"file": ("big.txt", b"way too many bytes here",
                                    "text/plain")})
    assert r.status_code == 400


def test_vision_setting_toggles_capability(app_client):
    client, _ = app_client
    client.put("/api/runtime/llm-settings", json={"vision_capable": 1})
    from aiforge_core.runtime import chat_media
    assert chat_media.vision_enabled("chat") is True
    client.put("/api/runtime/llm-settings", json={"vision_capable": 0})
    assert chat_media.vision_enabled("chat") is False


def test_vision_probe_accepts_and_rejects(app_client, monkeypatch):
    """No hardcoded allowlist — capability is PROBED from the endpoint (via the
    raw _post path so the HTTP error BODY reaches the classifier). Accepts the
    image → vision; definitively rejects image content → not."""
    import types

    from aiforge_core.llm import router
    from aiforge_core.runtime import chat_media, vision_detect
    monkeypatch.setattr(router, "resolve",
                        lambda role: types.SimpleNamespace(
                            model=role, base_url="http://x", api_key=""))

    # Accepting server → vision True (cached per model).
    chat_media.reset_vision_cache()
    monkeypatch.setattr(vision_detect, "probe_vision_endpoint",
                        lambda *a, **k: True)
    assert chat_media._probe_vision("some-served-model", "chat") is True
    assert chat_media._probe_vision("some-served-model", "chat") is True  # cached

    # Rejecting server (definitive "does not support image") → vision False.
    chat_media.reset_vision_cache()
    monkeypatch.setattr(vision_detect, "probe_vision_endpoint",
                        lambda *a, **k: False)
    assert chat_media._probe_vision("text-only-model", "chat") is False


def test_analyze_attachment_handles_image_and_document(app_client):
    """The shared analyser used by Jira/Confluence handles BOTH: a vision
    caption for an image, extracted text for a document."""
    from aiforge_core.runtime import chat_media
    # Document (text) → extracted text becomes the description.
    doc = chat_media.analyze_attachment("readme.txt", b"build then deploy",
                                        mime="text/plain")
    assert doc["filename"] == "readme.txt" and "build then deploy" in doc["description"]
    # Image → routed to the vision path (no model in test → empty), but no crash.
    img = chat_media.analyze_attachment("p.png", _PNG, mime="image/png")
    assert img["filename"] == "p.png" and isinstance(img["description"], str)
    # supported_attachment recognises images + docs, rejects video.
    assert chat_media.supported_attachment("image/png", "a.png")
    assert chat_media.supported_attachment("application/pdf", "a.pdf")
    assert chat_media.supported_attachment("", "a.docx")
    assert not chat_media.supported_attachment("video/mp4", "a.mp4")


def test_docx_extracts_paragraphs_and_tables(tmp_path):
    """docx extraction pulls BOTH paragraphs and tables, in document order —
    a plain ``.paragraphs`` pass silently drops every table."""
    docx = pytest.importorskip("docx")
    from aiforge_core.runtime import chat_media
    d = docx.Document()
    d.add_paragraph("intro para")
    t = d.add_table(rows=2, cols=2)
    t.rows[0].cells[0].text = "h1"; t.rows[0].cells[1].text = "h2"
    t.rows[1].cells[0].text = "v1"; t.rows[1].cells[1].text = "v2"
    d.add_paragraph("outro para")
    p = tmp_path / "doc.docx"
    d.save(str(p))
    txt = chat_media.extract_text(str(p))
    assert "intro para" in txt and "outro para" in txt      # paragraphs
    assert "h1 | h2" in txt and "v1 | v2" in txt             # table rows
    assert txt.index("intro") < txt.index("h1 | h2") < txt.index("outro")  # order


def test_docx_char_budget_truncates(tmp_path, monkeypatch):
    """A huge docx stops at the char budget instead of loading unbounded text."""
    docx = pytest.importorskip("docx")
    from aiforge_core.runtime import chat_media
    monkeypatch.setenv("AIFORGE_DOC_MAX_CHARS", "200")
    monkeypatch.setenv("AIFORGE_APPROX_PAGE_CHARS", "800")
    d = docx.Document()
    for i in range(500):
        d.add_paragraph(f"paragraph number {i} with filler content")
    p = tmp_path / "big.docx"
    d.save(str(p))
    txt = chat_media.extract_text(str(p))
    assert "budget reached" in txt
    # ~20k chars of text; the 200-char budget stops after the first ~800-char
    # page — bounded, nowhere near the full doc.
    assert len(txt) < 2000


def test_pdf_page_cap_and_budget_env(monkeypatch):
    """PDF page cap + char budget are env-tunable for very large (100+ page)
    docs — defaults are generous, overrides are honoured."""
    from aiforge_core.runtime import chat_media
    monkeypatch.setenv("AIFORGE_DOC_MAX_PAGES", "150")
    monkeypatch.setenv("AIFORGE_DOC_MAX_CHARS", "999")
    assert chat_media._pdf_page_cap() == 150
    assert chat_media._doc_char_budget() == 999


def test_pdf_tables_formats_rows(monkeypatch):
    """pdfplumber tables render as `a | b` rows, None cells blanked."""
    from aiforge_core.runtime import doc_extract

    class _Page:
        def extract_tables(self):
            return [[["h1", "h2"], ["v1", None]]]

    class _PDF:
        pages = [_Page()]
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr("pdfplumber.open", lambda p: _PDF())
    tbl = doc_extract._pdf_tables("x.pdf", 10)
    assert tbl[0] == ["h1 | h2", "v1 | "]


def test_pdf_text_pages_and_tables_with_markers(monkeypatch):
    """extract_text tags each PDF page with a `--- page N ---` marker and
    interleaves its structured tables."""
    import types
    from aiforge_core.runtime import doc_extract
    pages = [types.SimpleNamespace(extract_text=lambda: "page one text"),
             types.SimpleNamespace(extract_text=lambda: "page two text")]
    monkeypatch.setattr("pypdf.PdfReader",
                        lambda p: types.SimpleNamespace(pages=pages))
    monkeypatch.setattr(doc_extract, "_pdf_tables",
                        lambda path, cap: {0: ["a | b", "c | d"]})
    txt = doc_extract.extract_text("x.pdf", "application/pdf")
    assert "--- page 1 ---" in txt and "--- page 2 ---" in txt   # page markers
    assert "page one text" in txt and "page two text" in txt     # per-page text
    assert "a | b" in txt                                        # table restored
    assert txt.index("page 1") < txt.index("a | b") < txt.index("page 2")


def test_pdf_images_extracted_and_dispatched(monkeypatch):
    import types
    from aiforge_core.runtime import chat_media
    img = types.SimpleNamespace(name="im.png", data=b"IMGBYTES")
    page = types.SimpleNamespace(images=[img])
    monkeypatch.setattr("pypdf.PdfReader",
                        lambda p: types.SimpleNamespace(pages=[page]))
    assert chat_media._pdf_images("x.pdf") == [("im.png", b"IMGBYTES")]
    # dispatch routes .pdf / application/pdf to the pdf extractor
    assert chat_media._embedded_images("f.pdf", "") == [("im.png", b"IMGBYTES")]
    assert chat_media._embedded_images(
        "f.pdf", "application/pdf") == [("im.png", b"IMGBYTES")]


def test_pdf_image_captions_wired(monkeypatch):
    """PDF embedded images flow through the same vision-caption path as docx."""
    from aiforge_core.runtime import chat_media
    monkeypatch.setattr(chat_media, "_pdf_images", lambda p: [("pic.png", b"X")])
    monkeypatch.setattr(chat_media, "describe_bytes",
                        lambda raw, role="doer", **kw: "a bar chart")
    out = chat_media._with_doc_images("r.pdf", "application/pdf", "body text", "chat")
    assert "EMBEDDED IMAGES:" in out and "a bar chart" in out


def test_pdf_scanned_detection():
    from aiforge_core.runtime import chat_media
    assert chat_media._pdf_is_scanned("s.pdf", "application/pdf", "")           # empty
    assert chat_media._pdf_is_scanned("s.pdf", "", "  \n ")                     # blank
    assert not chat_media._pdf_is_scanned("s.pdf", "", "x" * 500)              # has text
    assert not chat_media._pdf_is_scanned("d.docx", "", "")                    # not pdf


def test_pdf_ocr_transcribes_textless_pages(monkeypatch):
    """OCR only pages that have NO text layer but DO have an image; each is
    transcribed via the vision model (OCR prompt), page-marked."""
    import types
    from aiforge_core.runtime import chat_media
    # page 0: real text → skipped; page 1: no text + image → OCR'd.
    p0 = types.SimpleNamespace(extract_text=lambda: "already has text",
                               images=[types.SimpleNamespace(data=b"IMG0")])
    p1 = types.SimpleNamespace(extract_text=lambda: "   ",
                               images=[types.SimpleNamespace(data=b"IMG1BIG")])
    monkeypatch.setattr("pypdf.PdfReader",
                        lambda p: types.SimpleNamespace(pages=[p0, p1]))
    seen = {}
    def _fake_bytes(raw, role="doer", *, prompt="", max_tokens=200):
        seen["prompt"] = prompt
        return "transcribed line A\ntranscribed line B"
    monkeypatch.setattr(chat_media, "describe_bytes", _fake_bytes)
    out = chat_media._pdf_ocr("scan.pdf", "chat")
    assert "OCR page 2" in out                       # only the text-less page
    assert "transcribed line A" in out
    assert "OCR page 1" not in out                   # page with text skipped
    assert "Transcribe ALL text" in seen["prompt"]   # OCR prompt, not caption


def test_pdf_ocr_page_cap(monkeypatch):
    import types
    from aiforge_core.runtime import chat_media
    monkeypatch.setenv("AIFORGE_PDF_OCR_MAX_PAGES", "2")
    pages = [types.SimpleNamespace(extract_text=lambda: "",
                                   images=[types.SimpleNamespace(data=b"IMG")])
             for _ in range(6)]
    monkeypatch.setattr("pypdf.PdfReader",
                        lambda p: types.SimpleNamespace(pages=pages))
    monkeypatch.setattr(chat_media, "describe_bytes",
                        lambda raw, role="doer", **kw: "text")
    out = chat_media._pdf_ocr("scan.pdf", "chat")
    assert "OCR stopped at 2 pages" in out
