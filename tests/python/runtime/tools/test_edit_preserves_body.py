"""Editing a Confluence page or a Jira description keeps what the edit does
not touch.

The update tools used to PUT whatever the model sent as the WHOLE body: asked
to add a section, a model sends the section, and the page lost everything
else; asked to fix a line, it rewrote the page as Markdown and every table and
macro became a paragraph. Jira had the same PUT, plus a converter that turned
the issue's own numbered list ("# step") into "h1." headings."""
from __future__ import annotations

import pytest

from aiforge_core.runtime.tools import confluence as cf
from aiforge_core.runtime.tools import edit_merge as em
from aiforge_core.runtime.tools import jira
from aiforge_core.runtime.tools.jira_format import to_jira_wiki

PAGE = ("<h1>Release plan</h1><p>" + "Intro text. " * 30 + "</p>"
        "<h2>Scope</h2><p>Old scope.</p>"
        "<table><tbody><tr><th>Item</th><th>Owner</th></tr>"
        "<tr><td>API</td><td>Ravi</td></tr></tbody></table>"
        "<h2>Risks</h2><ac:structured-macro ac:name=\"info\">"
        "<ac:rich-text-body><p>Watch the DB.</p></ac:rich-text-body>"
        "</ac:structured-macro><h2>Dates</h2><p>Go-live 01-10-2026.</p>")


# ── the pure merge ─────────────────────────────────────────────────────────

def test_append_and_prepend_keep_the_page():
    out = em.merge(PAGE, "<p>new</p>", "append", kind="storage")
    assert out.startswith(PAGE) and out.endswith("<p>new</p>")
    out = em.merge(PAGE, "<p>top</p>", "prepend", kind="storage")
    assert out.startswith("<p>top</p>") and out.endswith(PAGE)


def test_replace_section_swaps_only_that_section():
    out = em.merge(PAGE, "<p>New scope.</p>", "replace_section",
                   kind="storage", section="scope")
    assert "<h2>Scope</h2><p>New scope.</p><h2>Risks</h2>" in out
    assert "Old scope" not in out and "<table>" not in out   # the section's own table
    assert "Watch the DB." in out and "Go-live" in out       # the rest untouched
    # a fragment with its own heading renames it
    out = em.merge(PAGE, "<h2>Timeline</h2><p>Q4</p>", "replace_section",
                   kind="storage", section="Dates")
    assert out.endswith("<h2>Timeline</h2><p>Q4</p>") and "Go-live" not in out


def test_a_section_runs_to_the_next_heading_of_its_level_or_higher():
    body = "<h2>A</h2><p>a</p><h3>A.1</h3><p>a1</p><h2>B</h2><p>b</p>"
    out = em.merge(body, "<p>x</p>", "replace_section", kind="storage", section="A")
    assert out == "<h2>A</h2><p>x</p><h2>B</h2><p>b</p>"


def test_section_errors_name_the_headings():
    with pytest.raises(em.EditError, match="release plan"):
        em.merge(PAGE, "<p>x</p>", "replace_section", kind="storage", section="Budget")


def test_replace_text_must_be_exact_and_unique():
    out = em.merge(PAGE, "Go-live 15-10-2026.", "replace_text", kind="storage",
                   find="Go-live 01-10-2026.")
    assert "Go-live 15-10-2026." in out and len(out) == len(PAGE)
    with pytest.raises(em.EditError, match="not found"):
        em.merge(PAGE, "x", "replace_text", kind="storage", find="nope")
    with pytest.raises(em.EditError, match="occurs"):
        em.merge(PAGE, "x", "replace_text", kind="storage", find="Intro text.")


def test_a_full_replace_that_drops_most_of_the_page_is_refused():
    with pytest.raises(em.EditError, match="refused.*replace_section"):
        em.apply_edit(PAGE, "<p>Just the new bit.</p>", {"mode": "replace"},
                      kind="storage")
    # the model has to say the loss is intended
    assert em.apply_edit(PAGE, "<p>x</p>", {"allow_loss": True},
                         kind="storage") == "<p>x</p>"


def test_a_full_rewrite_that_flattens_tables_and_macros_is_refused():
    flattened = PAGE.replace("<table>", "<p>").replace("<ac:structured-macro", "<div")
    with pytest.raises(em.EditError, match="table.*macro|macro.*table"):
        em.apply_edit(PAGE, flattened, {}, kind="storage")


def test_a_complete_rewrite_that_keeps_everything_passes():
    edited = PAGE.replace("Old scope.", "Scope agreed with finance.")
    assert em.apply_edit(PAGE, edited, {"mode": "replace"}, kind="storage") == edited


def test_short_pages_can_be_rewritten_freely():
    assert em.apply_edit("<p>draft</p>", "<p>final</p>", {}, kind="storage") == "<p>final</p>"


def test_unknown_mode():
    with pytest.raises(em.EditError, match="unknown mode"):
        em.merge(PAGE, "x", "upsert", kind="storage")


# ── Confluence tool ────────────────────────────────────────────────────────

@pytest.fixture
def page(monkeypatch):
    state = {"puts": []}

    def _request(method, path, params=None, body=None, **kw):
        if method == "GET":
            return {"ok": True, "data": {"id": "10", "title": "Plan",
                                         "version": {"number": 4},
                                         "body": {"storage": {"value": PAGE}}}}
        state["puts"].append(body)
        return {"ok": True, "data": {"id": "10", "_links": {"webui": "/x"}}}
    monkeypatch.setattr(cf, "_request", _request)
    return state


def _written(state) -> str:
    return state["puts"][-1]["body"]["storage"]["value"]


def test_update_appends_markdown_as_storage_and_keeps_the_page(page):
    out = cf.confluence_update({"id": "10", "mode": "append",
                                "body": "## Decisions\n- ship on Friday"})
    assert out["ok"] and out["version"] == 5
    body = _written(page)
    assert body.startswith(PAGE)
    assert body.endswith("<h2>Decisions</h2>\n<ul><li>ship on Friday</li></ul>")


def test_update_replaces_one_line_inline(page):
    cf.confluence_update({"id": "10", "mode": "replace_text",
                          "find": "Go-live 01-10-2026.", "body": "Go-live **15-10-2026**."})
    body = _written(page)
    assert "<p>Go-live <strong>15-10-2026</strong>.</p>" in body   # no nested <p>
    assert "<table>" in body and "ac:structured-macro" in body


def test_update_refuses_the_partial_overwrite_that_wiped_pages(page):
    out = cf.confluence_update({"id": "10", "body": "## Decisions\n- ship on Friday"})
    assert out["ok"] is False and "refused" in out["error"]
    assert page["puts"] == []                                     # nothing written


def test_update_reports_a_missing_section_without_writing(page):
    out = cf.confluence_update({"id": "10", "mode": "replace_section",
                                "section": "Budget", "body": "x"})
    assert out["ok"] is False and "not found" in out["error"]
    assert page["puts"] == []


def test_read_says_when_it_did_not_see_the_whole_page(monkeypatch):
    monkeypatch.setattr(cf, "_BODY_CAP", 50, raising=False)
    from aiforge_core.runtime.tools.confluence import _tools
    monkeypatch.setattr(_tools, "_BODY_CAP", 50)
    monkeypatch.setattr(cf, "_request", lambda *a, **k: {"ok": True, "data": {
        "id": "10", "title": "Plan", "body": {"storage": {"value": PAGE}},
        "version": {"number": 4}, "space": {"key": "ENG"}}})
    monkeypatch.setattr(_tools, "_read_attachments", lambda *a, **k: [])
    out = cf.confluence_read({"id": "10"})
    assert out["truncated"] is True and out["body_chars"] == len(PAGE)


# ── Jira ───────────────────────────────────────────────────────────────────

DESC = ("h2. Steps\n# open the app\n# log in\n# pay\n\nh2. Notes\n"
        + "Seen on prod. " * 30 + "\n||Env||Build||\n|prod|1.2|")


@pytest.fixture
def issue(monkeypatch):
    state = {"puts": []}

    def _request(method, path, params=None, body=None, **kw):
        if method == "GET":
            return {"ok": True, "data": {"fields": {"description": DESC}}}
        state["puts"].append(body)
        return {"ok": True, "data": {}}
    monkeypatch.setattr(jira, "_request", _request)
    monkeypatch.setattr(jira, "_issue_url", lambda k: f"https://j/browse/{k}")
    return state


def test_existing_wiki_markup_is_not_converted_again():
    assert to_jira_wiki(DESC) == DESC
    assert to_jira_wiki("# open\n# log in") == "# open\n# log in"   # a list, not h1s
    assert to_jira_wiki("# Title\nbody") == "h1. Title\nbody"        # a lone heading
    assert to_jira_wiki("## Heading\n- item **b**") == "h2. Heading\n* item *b*"


def test_jira_append_keeps_the_description(issue):
    out = jira.jira_update({"key": "ENG-1", "mode": "append",
                            "description": "## Fix\n- **root cause:** cache"})
    assert out["ok"]
    desc = issue["puts"][-1]["fields"]["description"]
    assert desc.startswith(DESC)
    assert desc.endswith("h2. Fix\n* *root cause:* cache")


def test_jira_section_edit(issue):
    jira.jira_update({"key": "ENG-1", "mode": "replace_section", "section": "Steps",
                      "description": "# open the app\n# pay"})
    desc = issue["puts"][-1]["fields"]["description"]
    assert desc.startswith("h2. Steps\n# open the app\n# pay\n\nh2. Notes")
    assert "||Env||Build||" in desc


def test_jira_partial_overwrite_is_refused(issue):
    out = jira.jira_update({"key": "ENG-1", "description": "Fixed in 1.3"})
    assert out["ok"] is False and "refused" in out["error"]
    assert issue["puts"] == []


def test_jira_raw_fields_description_is_guarded_too(issue):
    out = jira.jira_update({"key": "ENG-1", "fields": {"description": "x"}})
    assert out["ok"] is False and issue["puts"] == []


def test_jira_edit_without_a_description_does_not_read_it(monkeypatch):
    calls = []
    monkeypatch.setattr(jira, "_request", lambda m, p, **k: calls.append(m)
                        or {"ok": True, "data": {}})
    monkeypatch.setattr(jira, "_issue_url", lambda k: "u")
    jira.jira_update({"key": "ENG-1", "summary": "s"})
    assert calls == ["PUT"]


# ── review round 1 ─────────────────────────────────────────────────────────

LAYOUT = ("<ac:layout><ac:layout-section ac:type=\"two_equal\">"
          "<ac:layout-cell><h2>Goals</h2><p>g</p></ac:layout-cell>"
          "<ac:layout-cell><h2>Risks</h2><p>r</p></ac:layout-cell>"
          "</ac:layout-section></ac:layout>")


def test_a_section_never_crosses_its_layout_cell():
    out = em.merge(LAYOUT, "<p>new</p>", "replace_section", kind="storage",
                   section="Risks")
    assert out == LAYOUT.replace("<p>r</p>", "<p>new</p>")    # closers intact
    out = em.merge(LAYOUT, "<p>G2</p>", "replace_section", kind="storage",
                   section="Goals")
    assert out == LAYOUT.replace("<p>g</p>", "<p>G2</p>")     # both cells kept


def test_a_section_inside_a_macro_stops_at_the_macro():
    body = ("<ac:structured-macro ac:name=\"expand\"><ac:rich-text-body>"
            "<h3>Details</h3><p>old</p></ac:rich-text-body></ac:structured-macro>"
            "<h3>After</h3><p>keep</p>")
    out = em.merge(body, "<p>new</p>", "replace_section", kind="storage",
                   section="Details")
    assert out == body.replace("<p>old</p>", "<p>new</p>")


def test_a_heading_inside_a_code_macro_is_not_a_section():
    body = ("<h2>Setup</h2><ac:structured-macro ac:name=\"code\"><ac:plain-text-body>"
            "<![CDATA[<h2>Fake</h2>]]></ac:plain-text-body></ac:structured-macro>"
            "<h2>Next</h2><p>n</p>")
    with pytest.raises(em.EditError, match="not found"):
        em.merge(body, "x", "replace_section", kind="storage", section="Fake")
    out = em.merge(body, "<p>s</p>", "replace_section", kind="storage", section="Setup")
    assert out == "<h2>Setup</h2><p>s</p><h2>Next</h2><p>n</p>"


def test_a_wiki_heading_inside_a_code_block_is_not_a_section():
    desc = "h2. Repro\n{code}\nh3. not a heading\n{code}\nh2. Notes\nn"
    out = em.merge(desc, "steps", "replace_section", kind="wiki", section="Repro")
    assert out == "h2. Repro\nsteps\n\nh2. Notes\nn"


def test_replace_text_keeps_markup_and_entities_it_copied(page):
    cf.confluence_update({"id": "10", "mode": "replace_text",
                          "find": "<td>Ravi</td>", "body": "<td>Asha &amp; Ravi</td>"})
    assert "<td>Asha &amp; Ravi</td>" in _written(page)
    cf.confluence_update({"id": "10", "mode": "replace_text",
                          "find": "Old scope.", "body": "R&D owns my_var_name"})
    assert "<p>R&amp;D owns my_var_name</p>" in _written(page)


def test_an_empty_body_deletes_a_section(page):
    out = cf.confluence_update({"id": "10", "mode": "replace_section",
                                "section": "Dates", "body": ""})
    assert out["ok"] and _written(page).endswith("<h2>Dates</h2>")
    assert cf.confluence_update({"id": "10", "body": ""})["ok"] is False


def test_a_refused_description_does_not_move_the_status(issue, monkeypatch):
    moved = []
    monkeypatch.setattr(jira, "jira_transition",
                        lambda a, cwd=None: moved.append(a) or {"ok": True})
    out = jira.jira_update({"key": "ENG-1", "status": "Done", "description": "x"})
    assert out["ok"] is False and moved == [] and issue["puts"] == []


def test_one_more_numbered_step_stays_a_list_item(issue):
    jira.jira_update({"key": "ENG-1", "mode": "replace_section", "section": "Steps",
                      "description": "# open the app\n# log in\n# pay\n# get receipt"})
    jira.jira_update({"key": "ENG-1", "mode": "append", "description": "# one more"})
    assert issue["puts"][-1]["fields"]["description"].endswith("# one more")


def test_markdown_with_code_is_still_converted():
    md = "## Types\nUse `list[int | None]` and `{note}` here.\n**bold**"
    assert to_jira_wiki(md).startswith("h2. Types")
    assert "*bold*" in to_jira_wiki(md)
    assert to_jira_wiki("see [the doc|https://x.io/d]") == "see [the doc|https://x.io/d]"


def test_raw_fields_description_clear_and_conflict(issue):
    out = jira.jira_update({"key": "ENG-1", "fields": {"description": None},
                            "allow_loss": True})
    assert out["ok"] and issue["puts"][-1]["fields"]["description"] == ""
    jira.jira_update({"key": "ENG-1", "mode": "append", "description": "tail",
                      "fields": {"description": "RAW"}})
    desc = issue["puts"][-1]["fields"]["description"]
    assert desc.startswith(DESC) and "RAW" not in desc


def test_the_team_agent_tools_offer_the_edit_modes():
    import inspect
    from aiforge_core.runtime.doer_tools import _integrations as t
    for fn in (t.confluence_update, t.jira_update):
        params = inspect.signature(fn).parameters
        assert {"mode", "section", "find", "allow_loss"} <= set(params), fn.__name__
