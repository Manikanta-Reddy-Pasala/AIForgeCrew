"""The chat agent's code tools: read a slice, summarise a doc, rename a symbol.

Small tools, but each has a sharp edge. Reading lines has to answer for a file
shorter than the range asked for rather than raising. Summarising a document
is nearly always asked for by ATTACHMENT NAME, not by path, so resolution has
to reach the session's media folder — and an out-of-range page request must
come back saying how many pages the file actually has, or the model retries
blindly. Renaming a symbol defaults to a DRY RUN: it rewrites source files in
place, so the caller has to ask for that explicitly.
"""
from __future__ import annotations

import pytest

from aiforge_core.runtime.chat_agent._tools import _code as T


# ─── reading a slice of a file ─────────────────────────────────────────


@pytest.fixture()
def src(tmp_path):
    (tmp_path / "app.py").write_text("".join(f"line {i}\n" for i in range(1, 11)))
    return tmp_path


def test_a_line_range_is_returned_with_the_files_length(src):
    res = T._t_read_lines({"path": "app.py", "start": 2, "end": 4}, str(src))
    assert res["text"] == "line 2\nline 3\nline 4\n"
    assert res["total_lines"] == 10 and res["start"] == 2 and res["end"] == 4


def test_no_range_reads_the_whole_file(src):
    res = T._t_read_lines({"path": "app.py"}, str(src))
    assert res["start"] == 1 and res["end"] == 10


def test_an_end_past_the_last_line_stops_at_the_end(src):
    assert T._t_read_lines({"path": "app.py", "end": 99}, str(src))["end"] == 10


def test_a_start_past_the_end_answers_empty_rather_than_failing(src):
    res = T._t_read_lines({"path": "app.py", "start": 50}, str(src))
    assert res["ok"] is True and res["text"] == "" and res["total_lines"] == 10


def test_a_zero_or_negative_start_is_the_first_line(src):
    assert T._t_read_lines({"path": "app.py", "start": 0},
                           str(src))["text"].startswith("line 1")


def test_an_absolute_path_is_read_as_given(src):
    res = T._t_read_lines({"path": str(src / "app.py")}, "/elsewhere")
    assert res["total_lines"] == 10


def test_a_missing_file_says_so(src):
    assert "not found" in T._t_read_lines({"path": "ghost.py"}, str(src))["error"]


def test_a_directory_is_an_error_not_a_crash(src):
    assert T._t_read_lines({"path": "."}, str(src))["ok"] is False


# ─── the codegraph pass-throughs ───────────────────────────────────────


@pytest.mark.parametrize("tool,fn", [
    (T._t_codegraph_query, "codegraph_query"),
    (T._t_codegraph_callers, "codegraph_callers"),
    (T._t_codegraph_callees, "codegraph_callees"),
    (T._t_codegraph_impact, "codegraph_impact"),
    (T._t_codegraph_explore, "codegraph_explore"),
])
def test_each_codegraph_tool_reaches_its_implementation(tool, fn, monkeypatch):
    from aiforge_core.runtime.tools import codegraph
    monkeypatch.setattr(codegraph, fn,
                        lambda args, cwd: {"ok": True, "fn": fn, "cwd": cwd})
    assert tool({"q": "x"}, "/repo") == {"ok": True, "fn": fn, "cwd": "/repo"}


# ─── finding the document to summarise ─────────────────────────────────


@pytest.fixture()
def media(tmp_path):
    d = tmp_path / ".aiforge" / "media"
    d.mkdir(parents=True)
    (d / "2026-Q1-report.pdf").write_bytes(b"%PDF")
    return tmp_path


def test_an_attachment_is_found_by_name(media):
    """This is the common case — the model has a filename, not a path."""
    assert T._resolve_doc("2026-Q1-report.pdf", str(media)) \
        == str(media / ".aiforge" / "media" / "2026-Q1-report.pdf")


def test_a_partial_name_still_finds_the_attachment(media):
    assert T._resolve_doc("Q1-report", str(media)).endswith("2026-Q1-report.pdf")


def test_a_path_relative_to_the_workspace_wins(media):
    (media / "local.pdf").write_bytes(b"%PDF")
    assert T._resolve_doc("local.pdf", str(media)) == str(media / "local.pdf")


def test_an_absolute_path_is_taken_as_is(media, tmp_path):
    p = tmp_path / "elsewhere.pdf"
    p.write_bytes(b"%PDF")
    assert T._resolve_doc(str(p), str(media)) == str(p)


def test_a_document_that_is_nowhere_is_not_resolved(media):
    assert T._resolve_doc("missing.pdf", str(media)) is None


def test_a_session_with_no_attachments_resolves_nothing(tmp_path):
    assert T._resolve_doc("x.pdf", str(tmp_path)) is None


# ─── summarising it ────────────────────────────────────────────────────


@pytest.fixture()
def doc(monkeypatch):
    from aiforge_core.runtime import doc_extract, doc_summarize
    state: dict = {"pages": ["p1", "p2", "p3"], "kind": "", "spec_ok": True,
                   "summary": "the gist", "seen": {}}
    monkeypatch.setattr(doc_extract, "paginate",
                        lambda fp, s: (state["pages"], state["kind"]))
    monkeypatch.setattr(doc_extract, "parse_page_spec",
                        lambda spec, total: [1] if state["spec_ok"] else [])
    monkeypatch.setattr(doc_summarize, "summarize_document",
                        lambda fp, role=None, pages=None:
                        state["seen"].update(role=role, pages=pages)
                        or state["summary"])
    return state


def test_a_document_is_summarised_with_its_page_count(doc, media):
    res = T._t_summarize_doc({"path": "2026-Q1-report.pdf"}, str(media))
    assert res["ok"] is True and res["summary"] == "the gist"
    assert res["page_count"] == 3 and res["pages"] == "all"
    assert doc["seen"]["role"] == "chat"


def test_a_page_range_reaches_the_summariser(doc, media):
    """So a 400-page report can be read section by section."""
    res = T._t_summarize_doc({"file": "report", "pages": "1-2",
                              "role": "architect"}, str(media))
    assert doc["seen"]["pages"] == "1-2" and doc["seen"]["role"] == "architect"
    assert res["pages"] == "1-2"


def test_an_out_of_range_page_request_is_told_the_real_count(doc, media):
    """Otherwise the model just retries blindly."""
    doc["spec_ok"] = False
    res = T._t_summarize_doc({"filename": "report", "pages": "50-60"},
                             str(media))
    assert res["ok"] is False and res["page_count"] == 3
    assert "out of range" in res["error"] and "3 page(s)" in res["error"]


@pytest.mark.parametrize("kind,marker", [("approx", "APPROXIMATE"),
                                         ("word", "Word's own")])
def test_estimated_pagination_is_declared(doc, media, kind, marker):
    doc["kind"] = kind
    assert marker in T._t_summarize_doc({"path": "report"}, str(media))["note"]


def test_an_unreadable_document_says_what_was_asked_for(doc, media):
    doc["summary"] = ""
    res = T._t_summarize_doc({"path": "report", "pages": "2"}, str(media))
    assert res["ok"] is False and "nothing readable for pages 2" in res["error"]


def test_a_file_that_cannot_be_found_is_reported(doc, media):
    assert "file not found" in T._t_summarize_doc({"path": "ghost.pdf"},
                                                  str(media))["error"]


def test_a_call_with_no_filename_at_all(doc, media):
    assert T._t_summarize_doc({}, str(media))["error"] == "need a file path/name"


# ─── renaming a symbol ─────────────────────────────────────────────────


@pytest.fixture()
def project(tmp_path):
    (tmp_path / "a.py").write_text("old_name = 1\nprint(old_name)\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.ts").write_text("const x = old_name;\n")
    (tmp_path / "notes.md").write_text("old_name everywhere\n")
    for d in ("node_modules", ".git"):
        (tmp_path / d).mkdir()
        (tmp_path / d / "c.py").write_text("old_name\n")
    return tmp_path


def test_a_rename_is_a_dry_run_by_default(project):
    """It rewrites files in place, so the caller asks for that explicitly."""
    res = T._t_rename_symbol({"name": "old_name", "new_name": "new_name"},
                             str(project))
    assert res["dry_run"] is True and res["applied"] == 0
    assert res["total_occurrences"] == 3
    assert (project / "a.py").read_text().startswith("old_name")


def test_applying_it_rewrites_every_source_file(project):
    res = T._t_rename_symbol({"name": "old_name", "new_name": "new_name",
                              "dry_run": False}, str(project))
    assert res["applied"] == 3
    assert (project / "a.py").read_text() == "new_name = 1\nprint(new_name)\n"
    assert "new_name" in (project / "sub" / "b.ts").read_text()


def test_vendor_and_build_directories_are_never_touched(project):
    T._t_rename_symbol({"name": "old_name", "new_name": "n",
                        "dry_run": False}, str(project))
    assert (project / "node_modules" / "c.py").read_text() == "old_name\n"
    assert (project / ".git" / "c.py").read_text() == "old_name\n"


def test_only_source_files_are_considered(project):
    files = [h["file"] for h in T._t_rename_symbol(
        {"name": "old_name", "new_name": "n"}, str(project))["files"]]
    assert "notes.md" not in files


def test_a_substring_is_not_a_match(project):
    (project / "a.py").write_text("old_name_2 = 1\nold_nameX\n")
    res = T._t_rename_symbol({"name": "old_name", "new_name": "n"},
                             str(project))
    assert [h["file"] for h in res["files"]] == ["sub/b.ts"]
    assert res["total_occurrences"] == 1


def test_the_search_can_be_narrowed_to_a_subtree(project):
    res = T._t_rename_symbol({"name": "old_name", "new_name": "n",
                              "path": "sub"}, str(project))
    assert [h["file"] for h in res["files"]] == ["sub/b.ts"]


def test_both_names_are_required(project):
    for args in ({"name": "x"}, {"new_name": "y"}, {}):
        assert T._t_rename_symbol(args, str(project))["ok"] is False


def test_a_symbol_that_is_nowhere_changes_nothing(project):
    res = T._t_rename_symbol({"name": "ghost", "new_name": "n",
                              "dry_run": False}, str(project))
    assert res["files"] == [] and res["total_occurrences"] == 0


def test_an_unwritable_file_is_not_counted_as_renamed(project, monkeypatch):
    import builtins
    real = builtins.open

    def _open(path, mode="r", **kw):
        if "w" in mode:
            raise PermissionError("read-only")
        return real(path, mode, **kw)
    monkeypatch.setattr(builtins, "open", _open)
    assert T._rename_in_file(str(project / "a.py"), __import__("re").compile(
        r"\bold_name\b"), "n", dry=False) == 0


def test_an_unreadable_file_is_skipped(project, monkeypatch):
    import builtins
    monkeypatch.setattr(builtins, "open",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("io")))
    assert T._rename_in_file("/x.py", __import__("re").compile("x"), "n",
                             dry=True) == 0
