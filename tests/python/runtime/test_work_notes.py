"""work_notes standard format + note_curator learner pass.

Covers: render/parse roundtrip, link normalization (dedupe / relative md
file links for cross-refs / scheme filter), read-modify-write preservation
of unknown sections + body, the curator's fact-drift + Learnings audit trail
(stubbed jira_read), the note_curate path jail, and staleness math.
"""
from __future__ import annotations

import datetime as dt
import os

import pytest

from aiforge_core.runtime import note_curator, work_notes


@pytest.fixture
def workdir(monkeypatch, tmp_path):
    """Isolate the managed work root under tmp so the path jail + context
    folders never touch the real ~/.aiforge."""
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    return tmp_path


# ── render / parse roundtrip ─────────────────────────────────────────────

def test_render_parse_roundtrip():
    text = work_notes.render_note(
        "jira", "ENG-1", title="ENG-1 — fix the flux capacitor",
        source_url="https://jira.local/browse/ENG-1",
        objective="fix the flux capacitor",
        key_results=["capacitor fluxes", "no smoke"],
        facts=["status: To Do", "assignee: Marty"],
        links=["https://jira.local/browse/ENG-1",
               "https://wiki.local/pages/12345/Spec"],
        learnings=["2026-07-01: created (auto)"],
        body_md="Long description here.\n\nWith paragraphs.")
    p = work_notes.parse_note(text)
    fm, sec = p["frontmatter"], p["sections"]
    assert fm["kind"] == "jira" and fm["key"] == "ENG-1"
    assert fm["source_url"] == "https://jira.local/browse/ENG-1"
    assert fm["updated_at"]          # stamped
    assert p["title"] == "ENG-1 — fix the flux capacitor"
    assert sec["objective"] == "fix the flux capacitor"
    assert sec["key_results"] == ["capacitor fluxes", "no smoke"]
    assert sec["facts"] == ["status: To Do", "assignee: Marty"]
    # cross-ref to ANOTHER managed dossier became a relative MD FILE LINK
    # (a plain markdown link to that dossier's note file); own URL kept
    assert sec["links"] == [
        "https://jira.local/browse/ENG-1",
        "[confluence/12345](../../confluence/12345/dossier.md)"]
    assert fm["links"] == sec["links"]    # frontmatter mirrors the section
    assert sec["learnings"] == ["2026-07-01: created (auto)"]
    assert "Long description here." in p["body"]
    assert "With paragraphs." in p["body"]


def test_render_skips_empty_sections_and_is_deterministic():
    a = work_notes.render_note("web", "s1", title="T", updated_at="X",
                               facts=["engine: fetch"])
    b = work_notes.render_note("web", "s1", title="T", updated_at="X",
                               facts=["engine: fetch"])
    assert a == b                       # deterministic byte-for-byte
    assert "## Objective" not in a and "## Links" not in a
    assert "## Learnings" not in a and "## Key Results" not in a
    assert "## Facts" in a
    # section order is fixed when several are present; heading uses the
    # Google-OKR title case ("Key Results")
    full = work_notes.render_note(
        "web", "s1", title="T", objective="o", key_results=["k"],
        facts=["f"], links=["https://x.io/"], learnings=["l"])
    idx = [full.index(h) for h in ("## Objective", "## Key Results",
                                   "## Facts", "## Links", "## Learnings")]
    assert idx == sorted(idx)


def test_parse_tolerates_legacy_heading_case():
    """Notes written before the Google-OKR title-case switch say
    '## Key results' — the parser must still map them onto key_results."""
    p = work_notes.parse_note("# T\n\n## Key results\n\n- old style item\n")
    assert p["sections"]["key_results"] == ["old style item"]


def test_parse_tolerates_hand_edited_legacy_file():
    p = work_notes.parse_note("# Just a title\n\nfree text, no frontmatter\n")
    assert p["frontmatter"] == {} and p["title"] == "Just a title"
    assert "free text" in p["body"]
    # even broken YAML frontmatter must not raise
    p2 = work_notes.parse_note("---\n: : bad {yaml\n---\n# T\nbody")
    assert p2["frontmatter"] == {} and p2["title"] == "T"


# ── link normalization ───────────────────────────────────────────────────

def test_normalize_links_dedupe_md_refs_and_scheme_filter():
    links = [
        "https://jira.local/browse/OPS-9",       # other ticket → md file link
        "https://jira.local/browse/ENG-1",       # SELF → stays a URL
        "https://wiki.local/pages/77770/Doc",    # page → md file link
        "https://wiki.local/x?pageId=77770",     # same page, other shape → dupe
        "[[confluence/88880]]",                  # legacy wiki ref → upgraded
        "ftp://files.local/a",                   # bad scheme → dropped
        "javascript:alert(1)",                   # bad scheme → dropped
        "file:///etc/passwd",                    # bad scheme → dropped
        "https://jira.local/browse/OPS-9",       # exact dupe → dropped
        "  ", "",                                # blanks → dropped
    ]
    out = work_notes.normalize_links(links, "jira", "ENG-1")
    assert out == [
        "[jira/OPS-9](../../jira/OPS-9/dossier.md)",
        "https://jira.local/browse/ENG-1",
        "[confluence/77770](../../confluence/77770/dossier.md)",
        "[confluence/88880](../../confluence/88880/dossier.md)"]


def test_normalize_links_confluence_self_stays_url():
    out = work_notes.normalize_links(
        ["https://wiki.local/pages/77770/Doc"], "confluence", "77770")
    assert out == ["https://wiki.local/pages/77770/Doc"]


def test_normalize_links_md_ref_is_stable_and_dedupes_all_spellings():
    """An already-canonical md file link stays byte-identical, and the URL /
    legacy-wiki / label-drifted-md spellings of the SAME target collapse
    into that one entry."""
    md = "[confluence/77770](../../confluence/77770/dossier.md)"
    out = work_notes.normalize_links(
        [md, "[[confluence/77770]]",
         "https://wiki.local/pages/77770/Doc",
         "[my label](../../confluence/77770/dossier.md)"],
        "jira", "ENG-1")
    assert out == [md]


# ── update_note: preservation + atomicity ────────────────────────────────

def test_update_preserves_unknown_sections_and_body(tmp_path):
    path = str(tmp_path / "ticket.md")
    text = work_notes.render_note(
        "jira", "ENG-1", title="ENG-1 — t", facts=["status: To Do"],
        body_md="the full ticket text")
    # simulate a hand-added custom section between managed content
    text += "\n## My scratch analysis\n\n- keep me exactly\n"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    r = work_notes.update_note(path, facts=["status: Done"])
    assert r["ok"]
    after = open(path, encoding="utf-8").read()
    assert "status: Done" in after and "status: To Do" not in after
    assert "## My scratch analysis" in after
    assert "- keep me exactly" in after
    assert "the full ticket text" in after
    assert not os.path.exists(path + ".tmp")     # atomic write cleaned up


def test_update_bumps_updated_at(tmp_path, monkeypatch):
    path = str(tmp_path / "n.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(work_notes.render_note(
            "web", "k", title="T", updated_at="2020-01-01T00:00:00+00:00"))
    assert work_notes.update_note(path)["ok"]
    fm = work_notes.parse_note(open(path, encoding="utf-8").read())["frontmatter"]
    assert fm["updated_at"] != "2020-01-01T00:00:00+00:00"


def test_update_note_soft_errors_on_missing_file(tmp_path):
    r = work_notes.update_note(str(tmp_path / "nope.md"), facts=["x"])
    assert r["ok"] is False and "read failed" in r["error"]


# ── curator: fact drift with a stubbed jira_read ─────────────────────────

def _write_jira_note(workdir, key="ENG-1", status="To Do"):
    from aiforge_core.runtime import work_context
    d = work_context.context_dir("jira", key)
    path = os.path.join(d, "ticket.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(work_notes.render_note(
            "jira", key, title=f"{key} — fix the bug",
            source_url=f"https://jira.local/browse/{key}",
            objective="fix the bug",
            facts=[f"status: {status}", "assignee: Marty"],
            links=[f"https://jira.local/browse/{key}"]))
    return path


def test_curator_status_change_updates_facts_and_learnings(workdir, monkeypatch):
    path = _write_jira_note(workdir)
    monkeypatch.setattr(
        "aiforge_core.runtime.tools.jira.jira_read",
        lambda args, cwd=None: {"ok": True, "key": "ENG-1",
                                "summary": "fix the bug",
                                "status": "In Progress",
                                "assignee": "Marty", "priority": ""})
    monkeypatch.setenv("AIFORGE_NOTE_LINK_CHECK", "0")   # no network
    r = note_curator.curate_note(path)
    assert r["ok"] and r["updated"]
    assert any("status To Do → In Progress" in c for c in r["changes"])
    after = work_notes.parse_note(open(path, encoding="utf-8").read())
    assert "status: In Progress" in after["sections"]["facts"]
    assert "assignee: Marty" in after["sections"]["facts"]   # unchanged kept
    learn = after["sections"]["learnings"]
    today = dt.date.today().isoformat()
    assert any(ln.startswith(today) and "status To Do → In Progress" in ln
               and "(auto-curated)" in ln for ln in learn)


def test_curator_no_drift_is_quiet_but_bumps_updated_at(workdir, monkeypatch):
    path = _write_jira_note(workdir, status="To Do")
    monkeypatch.setattr(
        "aiforge_core.runtime.tools.jira.jira_read",
        lambda args, cwd=None: {"ok": True, "summary": "fix the bug",
                                "status": "To Do", "assignee": "Marty",
                                "priority": ""})
    monkeypatch.setenv("AIFORGE_NOTE_LINK_CHECK", "0")
    before = work_notes.parse_note(
        open(path, encoding="utf-8").read())["frontmatter"]["updated_at"]
    # backdate so the bump is observable at second precision
    work_notes.update_note(path)
    r = note_curator.curate_note(path)
    assert r["ok"] and r["updated"] is False and r["changes"] == []
    after = work_notes.parse_note(open(path, encoding="utf-8").read())
    assert "learnings" not in after["sections"]      # no noise entries
    assert after["frontmatter"]["updated_at"] >= before


def test_curator_unconfigured_source_never_raises(workdir, monkeypatch):
    """Soft-error contract: jira not configured → curation still ok, facts
    untouched, no exception."""
    path = _write_jira_note(workdir)
    monkeypatch.setattr(
        "aiforge_core.runtime.tools.jira.jira_read",
        lambda args, cwd=None: {"ok": False, "error": "jira_not_configured"})
    monkeypatch.setenv("AIFORGE_NOTE_LINK_CHECK", "0")
    r = note_curator.curate_note(path)
    assert r["ok"] and r["updated"] is False


def test_curator_flags_dead_links(workdir, monkeypatch):
    path = _write_jira_note(workdir)
    monkeypatch.setattr(
        "aiforge_core.runtime.tools.jira.jira_read",
        lambda args, cwd=None: {"ok": False, "error": "jira_not_configured"})
    # every probed link reports a definitive 404
    monkeypatch.setattr(note_curator, "_link_dead", lambda url: True)
    r = note_curator.curate_note(path)
    assert r["ok"] and r["updated"]
    assert any(c.startswith("link dead:") for c in r["changes"])
    after = work_notes.parse_note(open(path, encoding="utf-8").read())
    assert after["sections"]["links"] == \
        ["https://jira.local/browse/ENG-1 (dead)"]
    # a second pass must not double-flag
    r2 = note_curator.curate_note(path)
    assert all("link dead" not in c for c in r2["changes"])


# ── path jail ────────────────────────────────────────────────────────────

def test_note_curate_refuses_paths_outside_work_root(workdir, tmp_path):
    outside = tmp_path / "elsewhere" / "note.md"
    outside.parent.mkdir(parents=True)
    outside.write_text(work_notes.render_note("jira", "X-1", title="t"),
                       encoding="utf-8")
    r = note_curator.curate_note(str(outside))
    assert r["ok"] is False and "work root" in r["error"]
    # the refused file is untouched
    assert "X-1" in outside.read_text(encoding="utf-8")


def test_note_curate_refuses_traversal_out_of_root(workdir):
    from aiforge_core.runtime import work_context
    d = work_context.context_dir("jira", "ENG-1")
    sneaky = os.path.join(d, "..", "..", "..", "..", "etc", "passwd")
    r = note_curator.curate_note(sneaky)
    assert r["ok"] is False


def test_chat_tool_note_curate_wired_and_jailed(workdir):
    from aiforge_core.runtime import chat_agent
    assert "note_curate" in chat_agent.TOOLS
    # it writes → must NOT be classified read-only (plan mode must block it)
    assert "note_curate" not in chat_agent._READONLY_TOOLS
    r = chat_agent.TOOLS["note_curate"]({"path": "/etc/hosts"}, str(workdir))
    assert r["ok"] is False


# ── staleness math ───────────────────────────────────────────────────────

def test_is_stale_math(monkeypatch):
    now = dt.datetime(2026, 7, 10, 12, 0, tzinfo=dt.timezone.utc)
    monkeypatch.setenv("AIFORGE_NOTE_STALE_HOURS", "24")
    fresh = (now - dt.timedelta(hours=23)).isoformat()
    stale = (now - dt.timedelta(hours=25)).isoformat()
    exact = (now - dt.timedelta(hours=24)).isoformat()
    assert note_curator.is_stale(fresh, now=now) is False
    assert note_curator.is_stale(stale, now=now) is True
    assert note_curator.is_stale(exact, now=now) is True   # >= threshold
    # naive timestamps are treated as UTC
    naive = (now - dt.timedelta(hours=25)).replace(tzinfo=None).isoformat()
    assert note_curator.is_stale(naive, now=now) is True
    # missing / garbage stamps are NOT stale (legacy files aren't churned)
    assert note_curator.is_stale("", now=now) is False
    assert note_curator.is_stale("not-a-date", now=now) is False
    # threshold <= 0 disables staleness entirely
    monkeypatch.setenv("AIFORGE_NOTE_STALE_HOURS", "0")
    assert note_curator.is_stale(stale, now=now) is False


def test_stale_note_path_for_bound_context(workdir, monkeypatch):
    monkeypatch.setenv("AIFORGE_NOTE_STALE_HOURS", "24")
    path = _write_jira_note(workdir)
    from aiforge_core.runtime import work_context
    cwd = work_context.context_dir("jira", "ENG-1")
    # freshly written → not stale
    assert note_curator.stale_note_path(cwd) is None
    # backdate the stamp → stale
    old = open(path, encoding="utf-8").read().replace(
        work_notes.parse_note(open(path, encoding="utf-8").read())
        ["frontmatter"]["updated_at"],
        "2020-01-01T00:00:00+00:00")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(old)
    assert note_curator.stale_note_path(cwd) == path
    # a non-context cwd never yields a note
    assert note_curator.stale_note_path(str(workdir)) is None


# ── intelligent consolidation (LLM map+dedupe → OKR) ─────────────────────

def _stub_structured(monkeypatch, handler):
    """Patch structured_complete to return an object shaped like the inner
    ConsolidatedNote (attr access), driven by `handler(user_json) -> dict`."""
    from types import SimpleNamespace as NS

    def fake(role, messages, response_model, **kw):
        user = next(m["content"] for m in messages if m["role"] == "user")
        d = handler(user)
        return NS(objective=d.get("objective", ""),
                  key_results=d.get("key_results", []),
                  facts=d.get("facts", []), links=d.get("links", []),
                  learnings=d.get("learnings", []))
    monkeypatch.setattr("aiforge_core.llm.structured.structured_complete", fake)


def test_consolidate_llm_maps_and_dedupes(monkeypatch):
    # the LLM folds new info: dedupes the paraphrased fact, supersedes status.
    def handler(user_json):
        payload = json.loads(user_json)
        cur = payload["current_sections"]
        assert "new_information" in payload
        return {"objective": cur.get("objective", ""),
                "key_results": cur.get("key_results", []),
                "facts": ["status: In Progress", "owner: Marty"],  # superseded
                "links": cur.get("links", []),
                "learnings": cur.get("learnings", [])}
    import json
    _stub_structured(monkeypatch, handler)
    existing = {"objective": "ship it", "facts": ["status: To Do",
                                                  "owner: Marty"]}
    out = work_notes.consolidate(existing, "moved to In Progress", role="learner")
    assert out["objective"] == "ship it"
    assert out["facts"] == ["status: In Progress", "owner: Marty"]
    assert "status: To Do" not in out["facts"]      # stale line dropped


def test_consolidate_falls_back_to_deterministic_merge(monkeypatch):
    # model down → new content lands as a deduped Fact, nothing lost.
    def boom(*a, **k):
        raise RuntimeError("model down")
    monkeypatch.setattr("aiforge_core.llm.structured.structured_complete", boom)
    existing = {"facts": ["a", "A"], "learnings": ["x"]}
    out = work_notes.consolidate(existing, "brand new fact", role="learner")
    assert out["facts"] == ["a", "brand new fact"]   # 'A' deduped, new appended
    assert out["learnings"] == ["x"]


def test_consolidate_empty_content_just_dedupes(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("no LLM call for empty content")
    monkeypatch.setattr("aiforge_core.llm.structured.structured_complete", boom)
    out = work_notes.consolidate({"facts": ["dup", "DUP", "keep"]}, "  ")
    assert out["facts"] == ["dup", "keep"]


def test_consolidate_chunks_large_input_and_folds(monkeypatch):
    # big input → multiple folds (chonkie or plain-slice). Each call appends its
    # chunk's fact; assert every chunk was folded in.
    import json
    calls = {"n": 0}

    def handler(user_json):
        calls["n"] += 1
        payload = json.loads(user_json)
        cur = payload["current_sections"]
        facts = list(cur.get("facts", []))
        facts.append(f"chunk-{calls['n']}")
        return {"objective": cur.get("objective", ""), "facts": facts,
                "key_results": [], "links": [], "learnings": []}
    _stub_structured(monkeypatch, handler)
    big = ("sentence. " * 4000)          # ~40k chars, exceeds the input budget
    out = work_notes.consolidate({}, big, role="learner", max_input_chars=8000)
    assert calls["n"] >= 2               # folded across multiple chunks
    assert any(f.startswith("chunk-") for f in out["facts"])


def test_consolidate_note_rewrites_file_and_preserves_body(monkeypatch, tmp_path):
    import json
    _stub_structured(monkeypatch, lambda uj: {
        **{k: json.loads(uj)["current_sections"].get(k, [])
           for k in ("key_results", "links", "learnings")},
        "objective": json.loads(uj)["current_sections"].get("objective", ""),
        "facts": json.loads(uj)["current_sections"].get("facts", [])
                 + ["consolidated: new insight"]})
    path = str(tmp_path / "dossier.md")
    text = work_notes.render_note("jira", "ENG-9", title="ENG-9 — t",
                                  facts=["status: To Do"],
                                  body_md="the full ticket text")
    text += "\n## My scratch analysis\n\n- keep me exactly\n"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    r = work_notes.consolidate_note(path, "found a new insight worth noting")
    assert r["ok"]
    after = open(path, encoding="utf-8").read()
    assert "consolidated: new insight" in after
    assert "status: To Do" in after                  # existing fact kept
    assert "## My scratch analysis" in after and "keep me exactly" in after
    assert "the full ticket text" in after
    assert not os.path.exists(path + ".tmp")


def test_chat_tool_note_consolidate_wired_and_jailed(workdir):
    from aiforge_core.runtime import chat_agent
    assert "note_consolidate" in chat_agent.TOOLS
    # it WRITES → must not be read-only (plan mode must block it)
    assert "note_consolidate" not in chat_agent._READONLY_TOOLS
    # missing text → soft error, no write
    assert chat_agent.TOOLS["note_consolidate"](
        {"path": "/etc/hosts"}, str(workdir))["ok"] is False
    # path outside the work root → refused
    r = chat_agent.TOOLS["note_consolidate"](
        {"text": "x", "path": "/etc/hosts"}, str(workdir))
    assert r["ok"] is False and "work root" in r["error"]
