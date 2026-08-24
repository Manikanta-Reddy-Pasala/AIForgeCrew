"""md_to_storage: agent Markdown → Confluence storage XHTML (so pages render
bold/headings/lists instead of literal '**'), leaving fences/images for
_storagify_media and passing already-storage bodies through untouched."""
from __future__ import annotations

from aiforge_core.runtime.tools.confluence_format import md_to_storage


def test_bold_italic_headings_lists():
    out = md_to_storage(
        "## **WiFi** APIs\n"
        "- **Endpoint:** POST /connect\n"
        "- plain item\n"
        "1. first\n"
        "2. second\n"
        "\nA para with **bold** and *italic*.\n")
    assert "<h2><strong>WiFi</strong> APIs</h2>" in out
    assert "<ul><li><strong>Endpoint:</strong> POST /connect</li>" \
        "<li>plain item</li></ul>" in out
    assert "<ol><li>first</li><li>second</li></ol>" in out
    assert "<p>A para with <strong>bold</strong> and <em>italic</em>.</p>" in out
    assert "**" not in out


def test_inline_code_and_link():
    out = md_to_storage("see `flag` at [docs](https://x.io)")
    assert "<code>flag</code>" in out
    assert '<a href="https://x.io">docs</a>' in out


def test_fences_and_images_left_for_storagify():
    out = md_to_storage("text\n\n```py\nx=1\n```\n\n![alt](http://img/a.png)")
    assert "```py\nx=1\n```" in out          # fence preserved verbatim
    assert "![alt](http://img/a.png)" in out  # image ref preserved


def test_already_storage_passes_through():
    body = "<p>already <strong>storage</strong></p>"
    assert md_to_storage(body) == body


def test_escapes_stray_angle_brackets():
    out = md_to_storage("compare a < b and 2 > 1")
    assert "&lt;" in out
    assert "&gt;" in out


def test_empty_and_none():
    assert md_to_storage("") == ""
    assert md_to_storage(None) is None
