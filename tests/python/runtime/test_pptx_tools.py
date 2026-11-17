"""PowerPoint support: pptx_read / pptx_create / pptx_edit and .pptx text
extraction for attachments.

python-pptx is an optional extra, so two things must hold at once: with it,
a deck made from an outline reads back the same and every edit op does what
it says; without it, the three tools answer with an install hint and the
rest (reading an attached deck as text, the registry, other tools) works.
"""
from __future__ import annotations

import sys

import pytest

from aiforge_core.runtime import doc_extract
from aiforge_core.runtime.chat_agent._tools import _pptx as P

pptx = pytest.importorskip("pptx")


def _png(path):
    from PIL import Image
    Image.new("RGB", (40, 20), (200, 30, 30)).save(path)
    return path


OUTLINE = [
    {"title": "Quarterly plan",
     "bullets": ["Grow revenue", ["Two new regions", {"text": "Pilot first", "level": 2}],
                 "Cut costs"],
     "notes": "Open with the goal."},
    {"title": "Owners", "table": [["Item", "Owner"], ["Hiring", "Ops"]]},
    {"title": "Chart", "bullets": ["See right"], "image": "pic.png"},
]


@pytest.fixture
def deck(tmp_path):
    _png(tmp_path / "pic.png")
    r = P._t_pptx_create({"path": "out/deck.pptx", "slides": OUTLINE}, str(tmp_path))
    assert r["ok"], r
    return tmp_path


def _read(tmp, path="out/deck.pptx", **kw):
    r = P._t_pptx_read({"path": path, **kw}, str(tmp))
    assert r["ok"], r
    return r


# ── create → read ───────────────────────────────────────────────────────
def test_create_then_read_round_trip(deck):
    r = _read(deck)
    assert r["slide_count"] == 3
    assert r["slide_size_in"] == [13.33, 7.5]
    s1, s2, s3 = r["slides"]
    assert [s["index"] for s in r["slides"]] == [1, 2, 3]
    assert s1["title"] == "Quarterly plan"
    body = next(sh["text"] for sh in s1["shapes"]
                if sh.get("placeholder") in ("body", "object"))
    assert body.splitlines() == ["Grow revenue", "  Two new regions",
                                 "    Pilot first", "Cut costs"]
    assert s1["notes"] == "Open with the goal."
    assert all(isinstance(sh["id"], int) and sh["name"] for sh in s1["shapes"])
    assert s2["has_table"] and not s2["has_image"]
    table = next(sh for sh in s2["shapes"] if sh["type"] == "table")
    assert table["rows"] == [["Item", "Owner"], ["Hiring", "Ops"]]
    assert s3["has_image"] and not s3["has_chart"]
    assert r["layouts"]


def test_new_deck_is_plain(deck):
    from pptx import Presentation
    prs = Presentation(str(deck / "out" / "deck.pptx"))
    slide = prs.slides[0]
    run = slide.shapes.title.text_frame.paragraphs[0].runs[0]
    assert run.font.name == "Calibri" and run.font.bold
    assert str(run.font.color.rgb) == "1F1F1F"
    assert str(slide.background.fill.fore_color.rgb) == "FFFFFF"
    bars = [sh for sh in slide.shapes if sh.name == P._ACCENT_BAR]
    assert len(bars) == 1 and str(bars[0].fill.fore_color.rgb) == P._ACCENT
    # the title spans the 16:9 width, not the 4:3 master's
    assert slide.shapes.title.width > prs.slide_width * 0.8
    # no empty "Click to add" placeholders left behind
    for s in prs.slides:
        for ph in s.placeholders:
            assert ph.text_frame.text.strip()


def test_create_reports_created_and_rejects_bad_input(tmp_path):
    r = P._t_pptx_create({"path": "a.pptx", "slides": '[{"title": "T"}]'},
                         str(tmp_path))
    assert r["ok"] and r["created"] and r["slides"] == 1
    again = P._t_pptx_create({"path": "a.pptx", "slides": [{"title": "U"}]},
                             str(tmp_path))
    assert again["ok"] and "created" not in again
    assert not P._t_pptx_create({"path": "b.pptx", "slides": []},
                                str(tmp_path))["ok"]
    miss = P._t_pptx_create({"path": "c.pptx",
                             "slides": [{"title": "x", "image": "nope.png"}]},
                            str(tmp_path))
    assert not miss["ok"] and "slide 1" in miss["error"] and "nope.png" in miss["error"]
    assert not (tmp_path / "c.pptx").exists()


def test_template_reuses_layouts_and_drops_its_slides(deck):
    r = P._t_pptx_create({"path": "t.pptx", "template": "out/deck.pptx",
                          "slides": [{"title": "Only", "bullets": ["one"]}]},
                         str(deck))
    assert r["ok"] and r["style"] == "template" and r["slides"] == 1
    out = _read(deck, "t.pptx")
    assert out["slide_count"] == 1 and out["slides"][0]["title"] == "Only"


def test_read_slide_range_and_attachment_lookup(deck):
    media = deck / ".aiforge" / "media"
    media.mkdir(parents=True)
    (media / "deck.pptx").write_bytes((deck / "out" / "deck.pptx").read_bytes())
    r = _read(deck, "deck.pptx", slides="2-3")
    assert [s["index"] for s in r["slides"]] == [2, 3]
    bad = P._t_pptx_read({"path": "deck.pptx", "slides": "9"}, str(deck))
    assert not bad["ok"] and "3 slide(s)" in bad["error"]
    assert not P._t_pptx_read({"path": "ghost.pptx"}, str(deck))["ok"]


def test_paths_respect_the_workspace_root(tmp_path, monkeypatch):
    from aiforge_core.runtime.chat_agent import _shell
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setattr(_shell, "_workspace_root", lambda: ws.resolve())
    r = P._t_pptx_create({"path": "../escape.pptx", "slides": [{"title": "x"}]},
                         str(ws))
    assert not r["ok"] and "escapes" in r["error"]
    assert not (tmp_path / "escape.pptx").exists()


# ── edit ops ────────────────────────────────────────────────────────────
def _edit(tmp, *ops, **kw):
    return P._t_pptx_edit({"path": "out/deck.pptx", "ops": list(ops), **kw},
                          str(tmp))


def test_replace_text_keeps_run_formatting(deck):
    r = _edit(deck, {"op": "replace_text", "find": "revenue", "replace": "income"})
    assert r["ok"] and "1 occurrence" in r["changes"][0]
    from pptx import Presentation
    prs = Presentation(str(deck / "out" / "deck.pptx"))
    run = prs.slides[0].placeholders[1].text_frame.paragraphs[0].runs[0]
    assert run.text == "Grow income" and run.font.name == "Calibri"
    # table cells are searched too
    assert _edit(deck, {"op": "replace_text", "find": "Hiring", "replace": "Hire"})["ok"]
    assert _read(deck)["slides"][1]["shapes"][-1]["rows"][1][0] == "Hire"


def test_replace_text_across_runs(deck):
    from pptx import Presentation
    path = deck / "out" / "deck.pptx"
    prs = Presentation(str(path))
    para = prs.slides[0].placeholders[1].text_frame.paragraphs[3]
    para.runs[0].text = "Cut co"
    para.add_run().text = "sts now"
    prs.save(str(path))
    r = _edit(deck, {"op": "replace_text", "find": "costs", "replace": "spend"})
    assert r["ok"], r
    body = _read(deck)["slides"][0]["shapes"]
    assert any("Cut spend now" in (sh.get("text") or "") for sh in body)


def test_set_title_bullets_notes(deck):
    r = _edit(deck,
              {"op": "set_title", "slide": 2, "text": "Who owns what"},
              {"op": "set_bullets", "slide": 1, "bullets": ["A", ["a1"]]},
              {"op": "set_notes", "slide": 3, "text": "Point at the chart."})
    assert r["ok"] and len(r["changes"]) == 3
    s = _read(deck)["slides"]
    assert s[1]["title"] == "Who owns what"
    body = next(sh["text"] for sh in s[0]["shapes"]
                if sh.get("placeholder") in ("body", "object"))
    assert body.splitlines() == ["A", "  a1"]
    assert s[2]["notes"] == "Point at the chart."


def test_add_slide_at_index_and_bullets_on_a_title_only_slide(deck):
    r = _edit(deck, {"op": "add_slide", "index": 1, "title": "Agenda",
                     "bullets": ["Plan", "Owners"]},
              {"op": "set_bullets", "slide": 3, "bullets": ["now has a body"]})
    assert r["ok"], r
    s = _read(deck)["slides"]
    assert [x["title"] for x in s] == ["Agenda", "Quarterly plan", "Owners", "Chart"]
    assert any("now has a body" in (sh.get("text") or "") for sh in s[2]["shapes"])
    # a slide added to a plain deck gets the plain geometry too
    from pptx import Presentation
    prs = Presentation(str(deck / "out" / "deck.pptx"))
    assert prs.slides[0].shapes.title.width > prs.slide_width * 0.8


def test_delete_and_move_slide(deck):
    r = _edit(deck, {"op": "move_slide", "slide": 3, "to": 1},
              {"op": "delete_slide", "slide": -1})
    assert r["ok"], r
    s = _read(deck)
    assert s["slide_count"] == 2
    assert [x["title"] for x in s["slides"]] == ["Chart", "Quarterly plan"]


def test_add_image_and_table(deck):
    r = _edit(deck, {"op": "add_image", "slide": 1, "image": "pic.png",
                     "left": 1, "top": 1, "width": 2},
              {"op": "add_table", "slide": 1, "rows": [["k", "v"], ["a", 1]]})
    assert r["ok"], r
    s1 = _read(deck)["slides"][0]
    assert s1["has_image"] and s1["has_table"]
    from pptx import Presentation
    pic = next(sh for sh in Presentation(str(deck / "out" / "deck.pptx")).slides[0].shapes
               if sh._element.tag.endswith("}pic"))
    assert pic.left == 914400 and pic.width == 2 * 914400


def test_out_path_leaves_the_source_alone(deck):
    before = (deck / "out" / "deck.pptx").read_bytes()
    r = _edit(deck, {"op": "set_title", "slide": 1, "text": "New"},
              out_path="copy.pptx")
    assert r["ok"] and r["path"].endswith("copy.pptx")
    assert (deck / "out" / "deck.pptx").read_bytes() == before
    assert _read(deck, "copy.pptx")["slides"][0]["title"] == "New"


def test_a_failing_op_saves_nothing(deck):
    before = (deck / "out" / "deck.pptx").read_bytes()
    r = _edit(deck, {"op": "set_title", "slide": 1, "text": "changed"},
              {"op": "delete_slide", "slide": 9})
    assert not r["ok"] and r["saved"] is False
    assert "op 2 (delete_slide)" in r["error"] and "3 slide(s)" in r["error"]
    assert r["applied_before_error"] == ["set_title: slide 1: title set"]
    assert (deck / "out" / "deck.pptx").read_bytes() == before
    unknown = _edit(deck, {"op": "explode"})
    assert not unknown["ok"] and "unknown op" in unknown["error"]
    missing = _edit(deck, {"op": "replace_text", "find": "zzz", "replace": "y"})
    assert not missing["ok"] and "not found" in missing["error"]


# ── without python-pptx ─────────────────────────────────────────────────
def test_missing_library_answers_with_the_install_hint(deck, monkeypatch):
    monkeypatch.setitem(sys.modules, "pptx", None)   # import pptx -> ImportError
    for fn, args in ((P._t_pptx_read, {"path": "out/deck.pptx"}),
                     (P._t_pptx_create, {"path": "x.pptx", "slides": [{"title": "t"}]}),
                     (P._t_pptx_edit, {"path": "out/deck.pptx",
                                       "ops": [{"op": "set_title", "slide": 1, "text": "t"}]})):
        assert fn(args, str(deck)) == {"ok": False, "error": P.MISSING}
    assert "pip install 'aiforgecrew[pptx]'" in P.MISSING
    assert not (deck / "x.pptx").exists()
    # reading an attached deck as text needs no python-pptx
    text = doc_extract.extract_text(str(deck / "out" / "deck.pptx"))
    assert "Quarterly plan" in text


# ── doc_extract on a .pptx ──────────────────────────────────────────────
def test_doc_extract_reads_slides_as_pages(deck):
    path = str(deck / "out" / "deck.pptx")
    pages, kind = doc_extract.paginate(path)
    assert kind == "slides" and len(pages) == 3
    assert pages[0].splitlines()[0] == "# slide 1: Quarterly plan"
    assert "Two new regions" in pages[0]
    assert "[notes]" in pages[0] and "Open with the goal." in pages[0]
    assert "Item | Owner" in pages[1] and "Hiring | Ops" in pages[1]
    assert "[1 image(s)]" in pages[2]
    text = doc_extract.extract_text(path)
    assert "--- page 2 ---" in text and "# slide 2: Owners" in text
    assert "Hiring" in doc_extract.extract_pages(path, "", "2")
    assert "Quarterly plan" not in doc_extract.extract_pages(path, "", "2")
    # by mime alone (an attachment saved without the extension)
    mime = doc_extract._EXT_MIME[".pptx"]
    bare = deck / "upload.bin"
    bare.write_bytes((deck / "out" / "deck.pptx").read_bytes())
    assert doc_extract.paginate(str(bare), mime)[1] == "slides"


def test_pptx_attachments_are_supported():
    from aiforge_core.runtime import chat_media
    assert chat_media.supported_attachment("", "talk.pptx")
    assert chat_media.supported_attachment(doc_extract._EXT_MIME[".pptx"], "x")


def test_a_broken_pptx_extracts_to_nothing(tmp_path):
    bad = tmp_path / "broken.pptx"
    bad.write_bytes(b"not a zip")
    assert doc_extract.extract_text(str(bad)) == ""
    assert doc_extract.paginate(str(bad)) == ([], "none")


# ── registration ────────────────────────────────────────────────────────
def test_tools_are_registered_and_described():
    from aiforge_core.runtime.chat_agent._registry import _READONLY_TOOLS, TOOLS
    from aiforge_core.runtime.chat_agent._tools import _schemas
    from aiforge_core.runtime.chat_agent._tools._families import family_of, named_families
    from aiforge_core.runtime.tools.tool_policy import _READONLY_ALWAYS_ALLOW
    for name in ("pptx_read", "pptx_create", "pptx_edit"):
        assert name in TOOLS and name in _schemas.CATALOG
        assert name in _schemas.NATIVE_TOOL_NAMES
        assert family_of(name) == "pptx"
    assert "pptx_read" in _READONLY_TOOLS and "pptx_read" in _READONLY_ALWAYS_ALLOW
    assert "pptx_edit" not in _READONLY_TOOLS
    assert "pptx" in named_families("make me a slide deck from these notes")
    assert "pptx" in named_families("edit report.pptx")
    picked = _schemas.filter_native(_schemas.NATIVE_TOOL_SCHEMAS,
                                    text="turn this into a PowerPoint")
    names = {s["function"]["name"] for s in picked}
    assert {"pptx_read", "pptx_create", "pptx_edit"} <= names
    plan = _schemas.filter_native(_schemas.NATIVE_TOOL_SCHEMAS, mode="plan",
                                  text="read the deck")
    plan_names = {s["function"]["name"] for s in plan}
    assert "pptx_read" in plan_names and "pptx_edit" not in plan_names
