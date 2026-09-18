import json

import pytest


def test_diff_preview_is_markdown_not_json_string():
    from aiforge_core.runtime import chat_agent as ca
    # integration write → readable markdown (heading + fields), not a JSON blob
    p = ca._diff_preview("jira_create",
                         {"project": "ENG", "summary": "Fix", "description": "## D"},
                         "/tmp")
    assert p.startswith("### Create Jira issue")
    # the body is previewed as what Jira will RENDER: converted to wiki the way
    # it is sent, then read back as Markdown ('## D' → 'h2. D' → a heading) —
    # never raw wiki markup the operator has to decode
    assert "**Project:**" in p
    assert "## D" in p and "h2. D" not in p
    assert not p.lstrip().startswith("{")        # NOT a raw json dump
    # command / diff → fenced code so the renderer shows monospace; an unknown
    # tool → a heading + its fields (a raw JSON dump was unreadable)
    assert "```bash" in ca._diff_preview("run_command", {"cmd": "ls"}, "/tmp")
    assert ca._diff_preview("weird_tool", {"a": 1}, "/tmp") == "### Weird tool\n\n**A:** `1`\n\n"
    gl = ca._diff_preview("gitlab_comment",
                          {"project": "g/p", "iid": 5, "body": "looks good"}, "/tmp")
    assert gl.startswith("### Comment on GitLab")
    assert "looks good" in gl


def test_xhtml_to_md_readable():
    from aiforge_core.runtime import chat_agent as ca
    out = ca._xhtml_to_md("<h2>Plan</h2><p>do <strong>x</strong> "
                          "<a href=\"http://x\">link</a></p><ul><li>a</li></ul>")
    assert "## Plan" in out
    assert "**x**" in out
    assert "[link](http://x)" in out
    assert "- a" in out
    assert "<" not in out          # no raw tags left


def test_confluence_create_preview_is_readable_not_xml_fence():
    from aiforge_core.runtime import chat_agent as ca
    p = ca._diff_preview("confluence_create",
                         {"space": "ENG", "title": "Doc", "body": "<h2>H</h2><p>t</p>"},
                         "/tmp")
    assert "## H" in p
    assert "```xml" not in p


def test_update_previews_show_a_diff(monkeypatch):
    from aiforge_core.runtime import chat_agent as ca
    import aiforge_core.runtime.tools.jira as jira
    monkeypatch.setattr(jira, "jira_read",
                        lambda a, c=None: {"ok": True, "summary": "old",
                                           "description": "old body"})
    p = ca._diff_preview("jira_update",
                         {"key": "ENG-1", "description": "new body"}, "/tmp")
    assert "```diff" in p
    assert "-old body" in p
    assert "+new body" in p


# ── approval previews read as formatted text, never a raw JSON dump ─────────

def test_a_confluence_comment_preview_is_readable_markdown():
    from aiforge_core.runtime.chat_agent._preview import _diff_preview
    md = _diff_preview("confluence_comment", {
        "id": "1844431465",
        "body": "<p><b>Co-Pilot — what this page needs</b></p>"
                "<p><b>1. Auth</b><br/>No mention of credentials.</p>"}, ".")
    assert md.startswith("### Comment on Confluence page `1844431465`")
    assert "Co-Pilot — what this page needs" in md
    assert "<p>" not in md and "\\u2014" not in md and '"body"' not in md


def test_a_markdown_comment_preview_stays_markdown():
    from aiforge_core.runtime.chat_agent._preview import _diff_preview
    md = _diff_preview("confluence_comment", {"id": "1", "body": "**Naming**\n\n1. one\n   - a"}, ".")
    assert md.endswith("**Naming**\n\n1. one\n   - a")


def test_a_jira_comment_preview_shows_what_jira_renders():
    from aiforge_core.runtime.chat_agent._preview import _diff_preview
    md = _diff_preview("jira_comment", {"key": "ENG-1", "body": "## Fix\n- **root cause:** cache"}, ".")
    assert "h2." not in md and "Fix" in md and "root cause" in md


def test_every_approval_gated_write_has_a_readable_preview():
    from aiforge_core.runtime.chat_agent._preview import _diff_preview
    samples = {
        "email_send": {"to": ["a@b.c"], "subject": "Hi", "body": "**Hello**"},
        "jira_transition": {"key": "E-1", "transition": "Done", "comment": "shipped"},
        "jira_link_issues": {"inward": "E-1", "outward": "E-2", "type": "Blocks"},
        "gitlab_mr_create": {"project": "p", "title": "T", "source_branch": "f"},
        "schedule_task": {"name": "nightly", "prompt": "x" * 200},
    }
    for tool, args in samples.items():
        md = _diff_preview(tool, args, ".")
        assert md.startswith("### "), (tool, md)
        assert not md.startswith("```json"), tool


def test_comments_are_converted_before_posting(monkeypatch):
    from aiforge_core.runtime.tools import confluence as cf
    from aiforge_core.runtime.tools import jira
    sent = []
    monkeypatch.setattr(cf, "_request", lambda m, p, **k: sent.append(k["body"]) or {"ok": True, "data": {}})
    cf.confluence_comment({"id": "1", "body": "**Bold** point\n\n- a"})
    assert sent[-1]["body"]["storage"]["value"] == "<p><strong>Bold</strong> point</p>\n<ul><li>a</li></ul>"
    jsent = []
    monkeypatch.setattr(jira, "_request", lambda m, p, **k: jsent.append(k.get("body")) or {"ok": True, "data": {}})
    jira.jira_link_issues({"inward": "E-1", "outward": "E-2", "comment": "**why:** it blocks"})
    assert jsent[-1]["comment"]["body"] == "*why:* it blocks"


def test_a_markdown_email_also_goes_out_as_html():
    from aiforge_core.runtime.tools import email_tool as et
    msg = et._build_message({"from": "me@x", "user": ""}, {"subject": "s",
                            "body": "## Plan\n- **one**\n\n```\nx < y\n```"}, ["a@b.c"], [])
    html = msg.get_body(preferencelist=("html",)).get_content()
    assert "<h2>Plan</h2>" in html and "<strong>one</strong>" in html
    assert "<pre><code>x &lt; y</code></pre>" in html
    plain = et._build_message({"from": "me@x", "user": ""}, {"subject": "s", "body": "Hi there."},
                              ["a@b.c"], [])
    assert plain.get_body(preferencelist=("html",)) is None


def test_jira_previews_show_what_jira_renders_even_without_headings():
    from aiforge_core.runtime.chat_agent._preview import _diff_preview
    md = _diff_preview("jira_comment", {"key": "E-1", "body": "1. Build it\n2. Test it\n\n**the bug** in `parse()`"}, ".")
    assert "# Build it" not in md and "{{" not in md
    assert "Build it" in md and "parse()" in md


def test_code_in_a_markdown_body_is_not_read_as_html():
    from aiforge_core.runtime.chat_agent._preview import _diff_preview
    body = "Wrap text in `<p>` and use `<br/>`.\n\n```html\n<div><b>New</b> &amp; improved</div>\n```"
    md = _diff_preview("gitlab_comment", {"project": "p", "iid": 1, "body": body}, ".")
    assert md.endswith(body)


def test_scripts_and_cron_lines_are_shown_exactly():
    from aiforge_core.runtime.chat_agent._preview import _diff_preview
    md = _diff_preview("create_job_script", {"name": "clean", "cron": "*/5 * * * *",
                                             "script": "# nightly cleanup\nrm -rf /tmp/*_old/*"}, ".")
    assert "`*/5 * * * *`" in md
    assert "```\n# nightly cleanup\nrm -rf /tmp/*_old/*\n```" in md


def test_the_email_preview_shows_every_part_sent():
    from aiforge_core.runtime.chat_agent._preview import _diff_preview
    md = _diff_preview("email_send", {"to": "a@b.c", "subject": "s", "body": "plain part",
                                      "html": '<a href="https://evil">https://bank.com</a>'}, ".")
    assert "plain part" in md and 'href="https://evil"' in md


def test_identifiers_urls_and_code_survive_the_confluence_conversion():
    from aiforge_core.runtime.tools.confluence_format import md_to_storage
    out = md_to_storage("Renamed `get_user_by_id` to `fetch_user`; see [PR](https://git.x/a_b/c_d) "
                        "and https://x.io/my_run_book — my_var_name, 2 * 3 * 4, **bold**, *it*, _em_")
    assert "<code>get_user_by_id</code>" in out and "<code>fetch_user</code>" in out
    assert 'href="https://git.x/a_b/c_d"' in out and "https://x.io/my_run_book" in out
    assert "my_var_name" in out and "2 * 3 * 4" in out
    assert "<strong>bold</strong>" in out and "<em>it</em>" in out and "<em>em</em>" in out


def test_a_comment_with_a_code_block_posts_well_formed_storage(monkeypatch):
    import xml.dom.minidom
    from aiforge_core.runtime.tools import confluence as cf
    sent = []
    monkeypatch.setattr(cf, "_request", lambda m, p, **k: sent.append(k["body"]) or {"ok": True, "data": {}})
    cf.confluence_comment({"id": "1", "body": "Fix:\n\n```python\nif a < b and c & d:\n    pass\n```"})
    value = sent[-1]["body"]["storage"]["value"]
    assert "ac:structured-macro" in value
    xml.dom.minidom.parseString(f'<r xmlns:ac="a" xmlns:ri="r">{value}</r>')   # well-formed


def test_plain_mail_with_one_bullet_or_a_quote_stays_plain():
    from aiforge_core.runtime.tools import email_tool as et
    assert et._markdown_html("Hi,\n- one thing\n\n> earlier reply\nThanks") == ""
    html = et._markdown_html("- a\n- b\n\n![x](https://track/p.png) <img src=https://t/p>")
    assert "<ul>" in html and "img" not in html.lower()


def test_links_keep_their_formatting_and_urls_their_emphasis():
    from aiforge_core.runtime.tools.confluence_format import md_to_storage
    out = md_to_storage("[`src/foo.py`](https://g.io/x) and [**big**](https://g.io/y); "
                        "see **https://jira/ONE-3** now, `__init__` and __init__")
    assert '<a href="https://g.io/x"><code>src/foo.py</code></a>' in out
    assert '<a href="https://g.io/y"><strong>big</strong></a>' in out
    assert "<strong>https://jira/ONE-3</strong>" in out
    assert "" not in out and "__init__" in out and "<strong>init</strong>" not in out


def test_email_keeps_code_that_shows_image_tags_and_entities():
    from aiforge_core.runtime.tools import email_tool as et
    html = et._markdown_html('## Snippet\n```html\n<img src="logo.png"> &amp; &lt;b&gt;\n```\n'
                             'Inline `![alt](url)` and a real ![px](https://t/p.png)')
    assert "&lt;img src=&quot;logo.png&quot;&gt; &amp;amp; &amp;lt;b&amp;gt;" in html
    assert "<code>![alt](url)</code>" in html and "t/p.png" not in html


def test_a_fence_inside_a_script_does_not_break_the_preview():
    from aiforge_core.runtime.chat_agent._preview import _diff_preview
    md = _diff_preview("create_job_script", {"script": "echo hi\n```\nrm -rf x\n```\nls"}, ".")
    assert "````\necho hi\n```\nrm -rf x\n```\nls\n````" in md
