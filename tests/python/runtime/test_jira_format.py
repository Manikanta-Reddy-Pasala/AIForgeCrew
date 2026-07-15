"""Jira Server/DC (REST v2) takes WIKI markup, not HTML — the agent's HTML or
Markdown comment/description must be converted so it doesn't render literally."""
from __future__ import annotations

from aiforge_core.runtime.tools.jira_format import to_jira_wiki


# ── HTML → wiki (the reported bug: <p><strong>…</strong><ul><li>…) ───────────
def test_html_bold_and_code_and_paragraph():
    out = to_jira_wiki("<p><strong>Technical Context:</strong> the "
                       "<code>.2</code> IP is used.</p>")
    assert "*Technical Context:*" in out
    assert "{{.2}}" in out
    assert "<p>" not in out and "<strong>" not in out and "<code>" not in out


def test_html_unordered_list():
    out = to_jira_wiki("<ul><li>first item</li><li>second item</li></ul>")
    lines = [l for l in out.splitlines() if l.strip()]
    assert lines == ["* first item", "* second item"]


def test_html_ordered_list_uses_hash():
    out = to_jira_wiki("<ol><li>step one</li><li>step two</li></ol>")
    lines = [l for l in out.splitlines() if l.strip()]
    assert lines == ["# step one", "# step two"]


def test_html_headings_links_entities():
    out = to_jira_wiki('<h3>Setup</h3><p>see <a href="http://x/y">docs</a> &amp; more</p>')
    assert "h3. Setup" in out
    assert "[docs|http://x/y]" in out
    assert "& more" in out                       # entity decoded
    assert "&amp;" not in out


def test_html_pre_block_becomes_code_macro():
    out = to_jira_wiki("<pre>line1\nline2</pre>")
    assert "{code}" in out and "line1" in out and "line2" in out


# ── Markdown → wiki (the normal case) ────────────────────────────────────────
def test_markdown_headings_bold_inline_code():
    out = to_jira_wiki("## Root Cause\n**bug** in the `parser` module")
    assert "h2. Root Cause" in out
    assert "*bug*" in out
    assert "{{parser}}" in out


def test_markdown_bullets_and_fence():
    out = to_jira_wiki("- one\n- two\n\n```py\nx=1\n```")
    lines = out.splitlines()
    assert "* one" in lines and "* two" in lines
    assert "{code:py}" in out and "x=1" in out and "{code}" in out


def test_markdown_link():
    assert "[docs|http://x]" in to_jira_wiki("see [docs](http://x)")


# ── passthrough / safety ─────────────────────────────────────────────────────
def test_plain_text_unchanged():
    assert to_jira_wiki("just a plain sentence.") == "just a plain sentence."


def test_empty_and_none():
    assert to_jira_wiki("") == ""
    assert to_jira_wiki(None) is None


def test_bold_inside_bullets_and_headings_becomes_single_star():
    """Regression: bold inside a bullet or heading was left as markdown '**'
    because inline conversion only ran on plain lines. Jira bold is '*x*'."""
    out = to_jira_wiki(
        "## **WiFi** APIs\n"
        "- **Endpoint:** POST /api/wifi/connect\n"
        "- plain **inline** bold\n"
        "1. **First** step\n")
    assert "**" not in out                       # no markdown double-star left
    assert "h2. *WiFi* APIs" in out
    assert "* *Endpoint:* POST /api/wifi/connect" in out
    assert "* plain *inline* bold" in out
    assert "# *First* step" in out


def test_inline_code_and_link_inside_bullet():
    out = to_jira_wiki("- see `flag` at [docs](http://x)")
    assert "* see {{flag}} at [docs|http://x]" in out
