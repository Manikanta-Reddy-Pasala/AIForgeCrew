"""Confluence and Jira bodies READ into markdown.

Both used to be pasted into dossiers raw — storage XHTML and wiki markup — so a
preview showed `<h2>Plan</h2>` or `h2. Plan`, and a Jira numbered list rendered
as a stack of top-level headings.
"""
from __future__ import annotations

from aiforge_core.runtime.tools.markup_read import (
    shift_headings,
    storage_to_md,
    truncate_md,
    wiki_to_md,
)

CONFLUENCE_PAGE = (
    '<h1>Release plan</h1>'
    '<p>Ship <strong>v2</strong> after <a href="https://x.io/q">QA</a>.</p>'
    '<h2>Steps</h2><ul><li>freeze</li><li>tag</li></ul>'
    '<table><tbody><tr><th>Env</th><th>Owner</th></tr>'
    '<tr><td>qa</td><td>ops | infra</td></tr></tbody></table>'
    '<ac:structured-macro ac:name="code" ac:schema-version="1">'
    '<ac:parameter ac:name="language">bash</ac:parameter>'
    '<ac:plain-text-body><![CDATA[if [ "$a" > 1 ]; then echo <ok>; fi]]>'
    '</ac:plain-text-body></ac:structured-macro>'
)

JIRA_DESCRIPTION = (
    "h2. Problem\n"
    "The *sync* job stalls on {{pendingToSync}}.\n"
    "\n"
    "h3. Steps\n"
    "# restart the pod\n"
    "# check [the dashboard|https://grafana.x/d/1]\n"
    "## nested step\n"
    "* a bullet\n"
    "{code:java}\n"
    "if (a * b > c) { run(); }\n"
    "{code}\n"
    "||Env||Status||\n"
    "|qa|red|\n"
    "{quote}\nfirst line\nsecond line\n{quote}\n"
)


# ── Confluence ───────────────────────────────────────────────────────────────
def test_confluence_headings_become_markdown_not_tags():
    md = storage_to_md(CONFLUENCE_PAGE)
    assert "# Release plan" in md
    assert "## Steps" in md
    assert "<h1>" not in md
    assert "<h2>" not in md


def test_confluence_inline_and_lists():
    md = storage_to_md(CONFLUENCE_PAGE)
    assert "**v2**" in md
    assert "[QA](https://x.io/q)" in md
    assert "- freeze" in md


def test_confluence_table_is_a_real_table_not_run_on_text():
    md = storage_to_md(CONFLUENCE_PAGE)
    assert "| Env | Owner |" in md
    assert "|---|---|" in md
    assert r"| qa | ops \| infra |" in md          # a pipe in a cell is escaped


def test_confluence_code_macro_keeps_angle_brackets():
    """The code body is CDATA containing < and >. A generic tag strip ate it."""
    md = storage_to_md(CONFLUENCE_PAGE)
    assert "```bash" in md
    assert 'if [ "$a" > 1 ]; then echo <ok>; fi' in md


# ── Jira ─────────────────────────────────────────────────────────────────────
def test_jira_headings_become_markdown():
    md = wiki_to_md(JIRA_DESCRIPTION)
    assert "## Problem" in md
    assert "### Steps" in md
    assert "h2." not in md
    assert "h3." not in md


def test_a_jira_numbered_list_is_not_a_stack_of_headings():
    """`# item` is a numbered list in Jira and an h1 in markdown."""
    md = wiki_to_md(JIRA_DESCRIPTION)
    assert "1. restart the pod" in md
    assert "   1. nested step" in md
    assert "# restart the pod" not in md


def test_jira_inline_markup():
    md = wiki_to_md(JIRA_DESCRIPTION)
    assert "**sync**" in md
    assert "`pendingToSync`" in md
    assert "[the dashboard](https://grafana.x/d/1)" in md
    assert "- a bullet" in md


def test_jira_code_block_is_fenced_and_left_alone():
    md = wiki_to_md(JIRA_DESCRIPTION)
    assert "```java\nif (a * b > c) { run(); }\n```" in md


def test_jira_table_and_quote():
    md = wiki_to_md(JIRA_DESCRIPTION)
    assert "| Env | Status |" in md
    assert "| qa | red |" in md
    assert "> first line\n> second line" in md


def test_arithmetic_is_not_mistaken_for_bold():
    assert wiki_to_md("2 * 3 * 4 = 24") == "2 * 3 * 4 = 24"


def test_plain_text_passes_through():
    assert wiki_to_md("just a sentence.") == "just a sentence."
    assert wiki_to_md("") == ""


# ── structure helpers ────────────────────────────────────────────────────────
def test_headings_are_pushed_below_their_section():
    md = "# Top\ntext\n## Sub\n```\n# not a heading\n```"
    out = shift_headings(md, 3)
    assert "### Top" in out
    assert "#### Sub" in out
    assert "# not a heading" in out            # code fences are untouched
    assert "### not a heading" not in out


def test_headings_are_never_promoted():
    assert shift_headings("#### deep", 2) == "#### deep"


def test_headings_cap_at_h6():
    assert shift_headings("# a\n###### f", 3).split("\n")[1] == "###### f"


def test_truncation_closes_an_open_fence():
    md = "intro\n```\n" + "line\n" * 500
    out = truncate_md(md, 200)
    assert out.count("```") % 2 == 0, "a cut left the rest of the dossier inside a code block"
    assert "truncated" in out


def test_a_pre_block_keeps_its_literal_text():
    md = storage_to_md("<pre>a &lt; b &amp;&amp; echo &lt;x&gt;</pre><p>after</p>")
    assert "a < b && echo <x>" in md          # decoded ONCE, then left alone
    assert "after" in md
