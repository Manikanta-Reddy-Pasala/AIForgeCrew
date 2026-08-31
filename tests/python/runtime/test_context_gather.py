"""Cross-entity dossiers: a ticket plus the pages it references, or vice versa.

The gather is cached against the entity's live ``updated`` stamp, and the two
edges of that cache are what matter. A cache is REUSED when the freshness
probe is inconclusive (Jira unreachable) rather than thrown away and re-fetched
into an error. And a gather where a LINKED fetch failed is written and usable
but NOT stamped as authoritative, so the next request tries again instead of
serving a permanently incomplete dossier.

Everything below stubs the Jira/Confluence tools; no network, no config.
"""
from __future__ import annotations

import json
import os

import pytest

from aiforge_core.runtime import context_gather as cg


@pytest.fixture()
def ctx(monkeypatch, tmp_path):
    from aiforge_core.runtime import work_context as wc
    from aiforge_core.runtime import work_notes

    def _dir(kind, key):
        d = tmp_path / kind / key
        d.mkdir(parents=True, exist_ok=True)
        return str(d)
    monkeypatch.setattr(wc, "context_dir", _dir)
    monkeypatch.setattr(work_notes, "render_note",
                        lambda kind, key, **kw: f"NOTE {kind}:{key}\n"
                                                f"{kw.get('body_md', '')}")
    monkeypatch.setattr(cg, "_capture_dossier", lambda *a: None)
    monkeypatch.setattr(cg, "_primary_updated", lambda kind, key: "2026-01-01")
    return tmp_path


# ─── small helpers ─────────────────────────────────────────────────────


@pytest.mark.parametrize("raw,slug", [
    ("ENG-1", "ENG-1"), ("a b/c", "a_b_c"), ("", "x"), ("///", "x"),
])
def test_identifiers_are_slugged_for_filenames(raw, slug):
    assert cg._slug(raw) == slug


def test_an_unwritable_artifact_is_skipped(tmp_path):
    cg._write(str(tmp_path / "missing" / "a.md"), "body")   # must not raise


def test_a_missing_or_corrupt_meta_file_reads_as_empty(tmp_path):
    assert cg._load_json(str(tmp_path / "gone.json")) == {}
    (tmp_path / "bad.json").write_text("{not json")
    assert cg._load_json(str(tmp_path / "bad.json")) == {}


# ─── the freshness probe ───────────────────────────────────────────────


def test_the_jira_updated_stamp_is_probed_cheaply(monkeypatch):
    from aiforge_core.runtime.tools import jira
    seen: dict = {}

    def _request(method, path, params=None, **kw):
        seen.update(path=path, params=params)
        return {"ok": True, "data": {"fields": {"updated": "2026-01-02"}}}
    monkeypatch.setattr(jira, "_request", _request)
    assert cg._primary_updated("jira", "ENG-1") == "2026-01-02"
    assert seen["params"] == {"fields": "updated"}      # one light field only


def test_the_confluence_version_stamp_is_probed(monkeypatch):
    from aiforge_core.runtime.tools import confluence
    monkeypatch.setattr(confluence, "_request",
                        lambda m, p, params=None, **kw: {
                            "ok": True, "data": {"version": {"when": "2026-01-03"}}})
    assert cg._primary_updated("confluence", "123") == "2026-01-03"


def test_an_unreachable_instance_has_no_stamp(monkeypatch):
    from aiforge_core.runtime.tools import jira
    monkeypatch.setattr(jira, "_request",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("offline")))
    assert cg._primary_updated("jira", "ENG-1") is None


def test_a_failed_probe_has_no_stamp(monkeypatch):
    from aiforge_core.runtime.tools import jira
    monkeypatch.setattr(jira, "_request", lambda *a, **k: {"ok": False})
    assert cg._primary_updated("jira", "ENG-1") is None


def test_an_unknown_kind_has_no_stamp():
    assert cg._primary_updated("gitlab", "x") is None


# ─── caching ───────────────────────────────────────────────────────────


@pytest.fixture()
def cached(tmp_path):
    (tmp_path / "dossier.md").write_text("the dossier")
    return str(tmp_path), str(tmp_path / "dossier.md")


def test_an_unchanged_entity_serves_the_cache(cached):
    base, md = cached
    out = cg._cached_dossier(base, md, {"updated": "2026-01-01",
                                        "artifacts": ["ticket.md"]},
                             "jira", "ENG-1", "2026-01-01", force=False)
    assert out["cached"] is True and out["dossier"] == "the dossier"
    assert out["artifacts"] == ["ticket.md"]


def test_an_inconclusive_probe_still_serves_the_cache(cached):
    """Offline is not a reason to throw a good cache away and error."""
    base, md = cached
    assert cg._cached_dossier(base, md, {"updated": "2026-01-01"}, "jira",
                              "ENG-1", None, force=False)["cached"] is True


def test_a_changed_entity_invalidates_the_cache(cached):
    base, md = cached
    assert cg._cached_dossier(base, md, {"updated": "2026-01-01"}, "jira",
                              "ENG-1", "2026-02-09", force=False) is None


def test_force_ignores_the_cache(cached):
    base, md = cached
    assert cg._cached_dossier(base, md, {"updated": "2026-01-01"}, "jira",
                              "ENG-1", "2026-01-01", force=True) is None


def test_no_meta_means_no_cache(cached):
    base, md = cached
    assert cg._cached_dossier(base, md, {}, "jira", "E-1", None, False) is None


def test_a_missing_dossier_file_means_no_cache(tmp_path):
    assert cg._cached_dossier(str(tmp_path), str(tmp_path / "gone.md"),
                              {"updated": "x"}, "jira", "E-1", "x", False) is None


def test_an_unreadable_dossier_serves_an_empty_body(tmp_path, monkeypatch):
    md = tmp_path / "dossier.md"
    md.write_text("x")
    real_open = open
    monkeypatch.setattr("builtins.open",
                        lambda p, *a, **k: (_ for _ in ()).throw(OSError("locked"))
                        if str(p).endswith("dossier.md") else real_open(p, *a, **k))
    out = cg._cached_dossier(str(tmp_path), str(md), {"updated": "x"}, "jira",
                             "E-1", "x", False)
    assert out["dossier"] == ""


# ─── link detection ────────────────────────────────────────────────────


def test_confluence_ids_come_from_links_and_from_the_text():
    ids = cg._detect_confluence_ids(
        {"description": "see https://wiki/pages/222222",
         "comments": [{"body": "and https://wiki/x?pageId=333333"}]},
        [{"confluence_page_id": "111111"}])
    assert ids == ["111111", "222222", "333333"]


def test_duplicate_page_ids_are_collapsed():
    ids = cg._detect_confluence_ids({"description": "/pages/111111 twice /pages/111111"},
                                    [{"confluence_page_id": "111111"}])
    assert ids == ["111111"]


def test_the_link_fan_out_is_capped():
    desc = " ".join(f"/pages/{100000 + i}" for i in range(30))
    assert len(cg._detect_confluence_ids({"description": desc}, [])) == cg._MAX_LINKS


def test_ticket_keys_are_detected_in_a_page_and_exclude_itself():
    keys = cg._detect_jira_keys({"title": "Spec for ENG-1",
                                 "body": "blocks OPS-42 and ENG-1"}, "ENG-1")
    assert keys == ["OPS-42"]


def test_the_ticket_fan_out_is_capped():
    body = " ".join(f"ENG-{i}" for i in range(30))
    assert len(cg._detect_jira_keys({"body": body}, "ENG-0")) == cg._MAX_LINKS


# ─── acceptance criteria ───────────────────────────────────────────────


@pytest.mark.parametrize("heading", [
    "h3. Acceptance criteria", "### Acceptance Criteria",
    "**Acceptance criteria**", "acceptance criteria:",
])
def test_acceptance_criteria_are_pulled_out_of_any_heading_style(heading):
    desc = f"intro\n\n{heading}\n* evicts the LRU\n- keeps 100 entries\n\nrest"
    assert cg._acceptance_criteria(desc) == ["evicts the LRU", "keeps 100 entries"]


def test_a_description_without_criteria():
    assert cg._acceptance_criteria("just prose") == []
    assert cg._acceptance_criteria("") == []


def test_the_criteria_list_is_capped():
    desc = "Acceptance criteria:\n" + "\n".join(f"- item {i}" for i in range(20))
    assert len(cg._acceptance_criteria(desc)) == 12


# ─── rendering ─────────────────────────────────────────────────────────


def test_a_ticket_renders_with_its_comments_and_attachments():
    md = cg._md_for("jira", {
        "key": "ENG-1", "summary": "Fix it", "status": "Open", "type": "Bug",
        "assignee": "Ada", "url": "https://j/ENG-1", "description": "details",
        "comments": [{"author": "Bo", "body": "looks right"}],
        "attachments": [{"filename": "shot.png", "description": "a chart"}]})
    assert md.startswith("# ENG-1 — Fix it")
    assert "> Bo: looks right" in md
    assert "[image/doc] shot.png — a chart" in md


def test_an_attachment_error_is_shown_in_place_of_a_description():
    md = cg._md_for("jira", {"attachments": [{"filename": "a.bin",
                                              "error": "unsupported"}]})
    assert "[image/doc] a.bin — unsupported" in md


def test_a_page_renders_with_a_capped_body():
    md = cg._md_for("confluence", {"title": "Spec", "url": "https://c/1",
                                   "body": "x" * 9000})
    assert md.startswith("# Spec")
    assert md.count("x") == 8000


def test_a_ticket_note_links_to_its_pages(ctx, monkeypatch):
    from aiforge_core.runtime import work_notes
    seen: dict = {}

    def _render(kind, key, **kw):
        seen.update(kw)
        return "NOTE"
    monkeypatch.setattr(work_notes, "render_note", _render)
    cg._note_for("jira", "ENG-1",
                 {"summary": "Fix it", "url": "https://j/ENG-1", "status": "Open",
                  "description": "Acceptance criteria:\n- works\n"},
                 [{"url": "https://wiki/pages/111111",
                   "confluence_page_id": "111111"}])
    assert "[[confluence/111111]]" in seen["links"]
    assert seen["key_results"] == ["works"]
    assert "status: Open" in seen["facts"]


def test_a_page_note_links_back_to_its_tickets(ctx, monkeypatch):
    from aiforge_core.runtime import work_notes
    seen: dict = {}
    monkeypatch.setattr(work_notes, "render_note",
                        lambda kind, key, **kw: seen.update(kw) or "NOTE")
    cg._note_for("confluence", "123", {"title": "Spec", "body": "for ENG-1",
                                       "space": "ENG", "version": 2})
    assert "[[jira/ENG-1]]" in seen["links"]
    assert "space: ENG" in seen["facts"]


def test_the_dossier_lists_its_linked_items(ctx, monkeypatch):
    from aiforge_core.runtime import work_notes
    seen: dict = {}
    monkeypatch.setattr(work_notes, "render_note",
                        lambda kind, key, **kw: seen.update(kw) or "DOSSIER")
    cg._render_dossier("jira", "ENG-1", {"summary": "Fix it", "url": "u"},
                       [("confluence", {"id": "111111", "title": "Spec"})])
    assert "[[confluence/111111]]" in seen["links"]
    assert seen["facts"] == ["linked items: 1"]
    assert "## Linked confluence" in seen["body_md"]


# ─── the parallel fan-out ──────────────────────────────────────────────


def test_every_linked_entity_is_read_and_written(tmp_path):
    def _reader(ident, role):
        return {"ok": True, "id": ident, "title": f"page {ident}"}
    artifacts: list = []
    secondaries, partial = _fan = cg._fan_out(_reader, ["111", "222"], "chat",
                                              "confluence", str(tmp_path),
                                              artifacts)
    assert partial is False and len(secondaries) == 2
    assert sorted(artifacts) == ["confluence-111.md", "confluence-222.md"]
    assert (tmp_path / "confluence-111.md").exists()


def test_a_failed_linked_read_marks_the_gather_partial(tmp_path):
    def _reader(ident, role):
        return {"ok": ident == "111", "id": ident}
    secondaries, partial = cg._fan_out(_reader, ["111", "222"], "chat",
                                       "confluence", str(tmp_path), [])
    assert partial is True and len(secondaries) == 1


def test_a_crashing_reader_marks_the_gather_partial(tmp_path):
    def _reader(ident, role):
        raise RuntimeError("timeout")
    secondaries, partial = cg._fan_out(_reader, ["111"], "chat", "confluence",
                                       str(tmp_path), [])
    assert partial is True and secondaries == []


# ─── the primary read ──────────────────────────────────────────────────


def test_a_ticket_read_fetches_its_links_in_parallel(monkeypatch):
    monkeypatch.setattr(cg, "_read_jira", lambda key, role: {"ok": True, "key": key})
    monkeypatch.setattr(cg, "_jira_links", lambda key: [{"url": "u"}])
    primary, links = cg._read_primary("jira", "ENG-1", "chat")
    assert primary["key"] == "ENG-1" and links == [{"url": "u"}]


def test_a_page_read_has_no_links_call(monkeypatch):
    monkeypatch.setattr(cg, "_read_confluence", lambda pid, role: {"ok": True})
    monkeypatch.setattr(cg, "_jira_links", lambda key: pytest.fail("asked jira"))
    assert cg._read_primary("confluence", "123", "chat") == ({"ok": True}, [])


def test_the_readers_delegate_to_the_tools(monkeypatch):
    from aiforge_core.runtime.tools import confluence, jira
    monkeypatch.setattr(jira, "jira_read", lambda args, cwd: {"ok": True, **args})
    monkeypatch.setattr(jira, "jira_remote_links",
                        lambda args, cwd: {"ok": True, "links": [{"url": "u"}]})
    monkeypatch.setattr(confluence, "confluence_read",
                        lambda args, cwd: {"ok": True, **args})
    assert cg._read_jira("ENG-1", "chat")["key"] == "ENG-1"
    assert cg._read_confluence("123", "chat")["id"] == "123"
    assert cg._jira_links("ENG-1") == [{"url": "u"}]


def test_failed_links_read_as_none(monkeypatch):
    from aiforge_core.runtime.tools import jira
    monkeypatch.setattr(jira, "jira_remote_links",
                        lambda args, cwd: {"ok": False, "error": "http 404"})
    assert cg._jira_links("ENG-1") == []


# ─── gather ────────────────────────────────────────────────────────────


def test_an_unsupported_kind_is_refused():
    assert cg.gather("gitlab", "x")["error"] == "unsupported kind 'gitlab'"


def test_a_complete_gather_writes_and_stamps_the_cache(ctx, monkeypatch):
    monkeypatch.setattr(cg, "_read_primary",
                        lambda kind, key, role: ({"ok": True, "key": key,
                                                  "summary": "Fix it",
                                                  "description": "/pages/111111"},
                                                 []))
    monkeypatch.setattr(cg, "_read_confluence",
                        lambda pid, role: {"ok": True, "id": pid, "title": "Spec"})
    out = cg.gather("jira", "ENG-1")
    assert out["ok"] is True and out["cached"] is False and out["linked"] == 1
    base = ctx / "jira" / "ENG-1"
    assert (base / "ticket.md").exists() and (base / "dossier.md").exists()
    meta = json.loads((base / ".dossier.json").read_text())
    assert meta["updated"] == "2026-01-01" and meta["partial"] is False
    assert meta["artifacts"] == ["ticket.md", "confluence-111111.md"]


def test_a_partial_gather_is_usable_but_not_stamped(ctx, monkeypatch):
    """Otherwise the next request serves a permanently incomplete dossier."""
    monkeypatch.setattr(cg, "_read_primary",
                        lambda kind, key, role: ({"ok": True, "key": key,
                                                  "description": "/pages/111111"}, []))
    monkeypatch.setattr(cg, "_read_confluence",
                        lambda pid, role: {"ok": False, "error": "http 404"})
    out = cg.gather("jira", "ENG-1")
    assert out["ok"] is True and out["linked"] == 0
    meta = json.loads((ctx / "jira" / "ENG-1" / ".dossier.json").read_text())
    assert meta["updated"] is None and meta["partial"] is True


def test_an_unreadable_primary_surfaces_the_reason(ctx, monkeypatch):
    monkeypatch.setattr(cg, "_read_primary",
                        lambda kind, key, role: ({"ok": False,
                                                  "error": "jira_not_configured"}, []))
    out = cg.gather("jira", "ENG-1")
    assert out == {"ok": False, "error": "jira_not_configured", "kind": "jira",
                   "key": "ENG-1", "dir": str(ctx / "jira" / "ENG-1")}
    assert not (ctx / "jira" / "ENG-1" / "dossier.md").exists()


def test_a_page_gathers_its_tickets(ctx, monkeypatch):
    monkeypatch.setattr(cg, "_read_primary",
                        lambda kind, key, role: ({"ok": True, "id": key,
                                                  "title": "Spec",
                                                  "body": "blocks ENG-7"}, []))
    monkeypatch.setattr(cg, "_read_jira",
                        lambda key, role: {"ok": True, "key": key})
    out = cg.gather("confluence", "123")
    assert out["linked"] == 1
    assert (ctx / "confluence" / "123" / "page.md").exists()
    assert (ctx / "confluence" / "123" / "jira-ENG-7.md").exists()


def test_a_second_gather_serves_the_cache(ctx, monkeypatch):
    monkeypatch.setattr(cg, "_read_primary",
                        lambda kind, key, role: ({"ok": True, "key": key}, []))
    cg.gather("jira", "ENG-1")
    monkeypatch.setattr(cg, "_read_primary",
                        lambda *a: pytest.fail("re-fetched an unchanged entity"))
    assert cg.gather("jira", "ENG-1")["cached"] is True


def test_a_refresh_is_flagged_as_such(ctx, monkeypatch):
    monkeypatch.setattr(cg, "_read_primary",
                        lambda kind, key, role: ({"ok": True, "key": key}, []))
    cg.gather("jira", "ENG-1")
    assert cg.gather("jira", "ENG-1", force=True)["refreshed"] is True


def test_the_dossier_is_captured_into_memory(monkeypatch):
    from aiforge_core.memory import md_store
    seen: dict = {}

    def _capture(kind, text, repo=None, topic=None):
        seen.update(kind=kind, text=text, repo=repo, topic=topic)
    monkeypatch.setattr(md_store, "capture", _capture)
    cg._capture_dossier("jira", "ENG-1", {"summary": "Fix it"}, [], "/base")
    assert seen["repo"] == "ENG-1" and seen["topic"] == "jira-dossier"
    assert "DOSSIER jira:ENG-1" in seen["text"]


def test_a_failed_capture_is_not_fatal(monkeypatch):
    from aiforge_core.memory import md_store
    monkeypatch.setattr(md_store, "capture",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    cg._capture_dossier("jira", "ENG-1", {}, [], "/base")
