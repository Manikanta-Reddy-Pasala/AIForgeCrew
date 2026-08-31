"""Chat-side skill/workflow authoring and the atomic multi-edit.

multi_edit is the piece with teeth: every edit is validated and applied to an
in-memory working copy FIRST, the resulting files are syntax-checked, and only
then is anything written — and if a write fails partway the already-written
files are rolled back. Without that a batch could leave the tree half-edited
and not compiling.

The per-edit refusals matter for the same reason: an ambiguous ``old_str``
(more than one occurrence) is refused rather than replacing an arbitrary one,
and an empty ``old_str`` — which would match everywhere — is refused outright.
"""
from __future__ import annotations

import pytest

from aiforge_core.runtime.chat_agent._tools import _skills as sk


@pytest.fixture(autouse=True)
def no_syntax_gate(monkeypatch):
    monkeypatch.setattr(sk, "_syntax_check", lambda ap, content, args: None)


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(sk, "_resolve", lambda cwd, path: tmp_path / path)
    (tmp_path / "a.py").write_text("x = 1\ny = 2\n")
    (tmp_path / "b.py").write_text("v = 1\nv = 1\n")
    return tmp_path


def _edit(path, old, new, **kw):
    return {"path": path, "old_str": old, "new_str": new, **kw}


# ─── skill + workflow authoring ────────────────────────────────────────


def test_skills_are_searched(monkeypatch):
    from aiforge_core.runtime import skills
    seen: dict = {}

    def _search(q, cwd, k=5):
        seen.update(q=q, cwd=cwd, k=k)
        return [{"name": "deploy"}]
    monkeypatch.setattr(skills, "search", _search)
    assert sk._t_skill_search({"q": "deploy", "k": 3}, "/repo") == {
        "ok": True, "skills": [{"name": "deploy"}]}
    assert seen == {"q": "deploy", "cwd": "/repo", "k": 3}


def test_a_broken_skill_registry_is_soft(monkeypatch):
    from aiforge_core.runtime import skills
    monkeypatch.setattr(skills, "search",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    assert sk._t_skill_search({"query": "x"}, "/repo") == {"ok": False,
                                                            "error": "down"}


def test_a_skill_is_authored_with_split_triggers(monkeypatch):
    from aiforge_core.runtime import skills
    seen: dict = {}
    monkeypatch.setattr(sk, "_elaborate_body",
                        lambda kind, body, name=None, description=None:
                        f"{kind}:{body}")
    monkeypatch.setattr(skills, "write_skill",
                        lambda **kw: seen.update(kw) or {"ok": True})
    sk._t_learn_skill({"name": "deploy", "description": "d", "body": "steps",
                       "triggers": "a, b ,, c", "scope": "REPO"}, "/repo")
    assert seen["triggers"] == ["a", "b", "c"] and seen["scope"] == "repo"
    assert seen["body"] == "skill:steps"


def test_the_alternate_body_key_is_accepted(monkeypatch):
    from aiforge_core.runtime import skills
    seen: dict = {}
    monkeypatch.setattr(sk, "_elaborate_body",
                        lambda kind, body, **kw: body)
    monkeypatch.setattr(skills, "write_skill",
                        lambda **kw: seen.update(kw) or {"ok": True})
    sk._t_learn_skill({"name": "n", "content": "from content"}, "/repo")
    assert seen["body"] == "from content"


def test_a_failed_skill_write_is_soft(monkeypatch):
    from aiforge_core.runtime import skills
    monkeypatch.setattr(sk, "_elaborate_body", lambda kind, body, **kw: body)
    monkeypatch.setattr(skills, "write_skill",
                        lambda **kw: (_ for _ in ()).throw(OSError("read-only")))
    assert sk._t_learn_skill({"name": "n", "body": "b"}, "/repo")["ok"] is False


def test_workflows_are_searched(monkeypatch):
    from aiforge_core.runtime import workflows
    monkeypatch.setattr(workflows, "search",
                        lambda q, cwd, k=5: [{"name": "release"}])
    assert sk._t_workflow_search({"query": "release"}, "/repo")["workflows"] == \
        [{"name": "release"}]


def test_a_workflow_is_authored_with_its_scripts(monkeypatch):
    """write_workflow hard-tests each script and refuses the save on failure —
    job-builder parity, no honour-system flag."""
    from aiforge_core.runtime import workflows
    seen: dict = {}
    monkeypatch.setattr(sk, "_elaborate_body", lambda kind, body, **kw: body)
    monkeypatch.setattr(workflows, "write_workflow",
                        lambda **kw: seen.update(kw) or {"ok": True})
    sk._t_learn_workflow({"name": "release", "body": "steps",
                          "scripts": [{"name": "run.sh"}], "triggers": ["ship"]},
                         "/repo")
    assert seen["scripts"] == [{"name": "run.sh"}] and seen["triggers"] == ["ship"]


def test_a_failed_workflow_write_is_soft(monkeypatch):
    from aiforge_core.runtime import workflows
    monkeypatch.setattr(sk, "_elaborate_body", lambda kind, body, **kw: body)
    monkeypatch.setattr(workflows, "write_workflow",
                        lambda **kw: (_ for _ in ()).throw(ValueError("script failed")))
    out = sk._t_learn_workflow({"name": "n", "body": "b"}, "/repo")
    assert out == {"ok": False, "error": "script failed"}


# ─── the editor bridge ─────────────────────────────────────────────────


def test_both_spellings_of_every_editor_field_are_accepted(monkeypatch):
    import aiforge_core.runtime.tools.editor as ed
    seen: dict = {}
    monkeypatch.setattr(ed, "editor", lambda **kw: seen.update(kw) or {"ok": True})
    sk._t_editor({"sub_command": "str_replace", "path": "a.py",
                  "content": "body", "old_text": "a", "new_text": "b",
                  "insert_line": "3", "view_range": ["1", "5"]}, "/repo")
    assert seen["command"] == "str_replace" and seen["file_text"] == "body"
    assert seen["old_str"] == "a" and seen["new_str"] == "b"
    assert seen["insert_line"] == 3 and seen["view_range"] == [1, 5]


def test_a_junk_view_range_is_dropped(monkeypatch):
    import aiforge_core.runtime.tools.editor as ed
    seen: dict = {}
    monkeypatch.setattr(ed, "editor", lambda **kw: seen.update(kw) or {"ok": True})
    sk._t_editor({"path": "a.py", "view_range": ["one", "5"]}, "/repo")
    assert seen["view_range"] is None and seen["command"] == "view"


# ─── staging one edit ──────────────────────────────────────────────────


def test_a_valid_edit_is_applied_to_the_working_copy(repo):
    batch = sk._EditBatch()
    assert sk._stage_edit(0, _edit("a.py", "x = 1", "x = 99"), batch, "/repo") is None
    assert "x = 99" in batch.pending[str(repo / "a.py")]
    assert (repo / "a.py").read_text() == "x = 1\ny = 2\n"     # not written yet


@pytest.mark.parametrize("edit,fragment", [
    ("not an object", "not an object"),
    ({"old_str": "a", "new_str": "b"}, "needs path"),
    ({"path": "a.py", "new_str": "b"}, "needs path"),
    ({"path": "a.py", "old_str": "", "new_str": "b"}, "must be non-empty"),
])
def test_a_malformed_edit_is_refused(repo, edit, fragment):
    out = sk._stage_edit(0, edit, sk._EditBatch(), "/repo")
    assert out["ok"] is False and fragment in out["error"]


def test_text_that_is_not_there(repo):
    out = sk._stage_edit(0, _edit("a.py", "zzz", "y"), sk._EditBatch(), "/repo")
    assert "old_str not found" in out["error"]


def test_a_missing_file(repo):
    out = sk._stage_edit(0, _edit("gone.py", "a", "b"), sk._EditBatch(), "/repo")
    assert "file not found" in out["error"]


def test_an_ambiguous_match_is_refused(repo):
    """Replacing an arbitrary one of several occurrences corrupts silently."""
    out = sk._stage_edit(0, _edit("b.py", "v = 1", "v = 2"), sk._EditBatch(), "/repo")
    assert "appears 2×" in out["error"] and "replace_all" in out["error"]


def test_replace_all_makes_it_unambiguous(repo):
    batch = sk._EditBatch()
    assert sk._stage_edit(0, _edit("b.py", "v = 1", "v = 2", replace_all=True),
                          batch, "/repo") is None
    assert batch.pending[str(repo / "b.py")] == "v = 2\nv = 2\n"


def test_a_path_outside_the_workspace_is_refused(repo, monkeypatch):
    monkeypatch.setattr(sk, "_resolve",
                        lambda cwd, path: (_ for _ in ()).throw(
                            PermissionError("outside the workspace")))
    out = sk._stage_edit(0, _edit("../x.py", "a", "b"), sk._EditBatch(), "/repo")
    assert out["ok"] is False and "outside" in out["error"]


def test_two_edits_to_one_file_chain(repo):
    batch = sk._EditBatch()
    sk._stage_edit(0, _edit("a.py", "x = 1", "x = 99"), batch, "/repo")
    sk._stage_edit(1, _edit("a.py", "y = 2", "y = 98"), batch, "/repo")
    assert batch.pending[str(repo / "a.py")] == "x = 99\ny = 98\n"


# ─── writing the batch ─────────────────────────────────────────────────


def test_a_clean_batch_writes_every_file(repo):
    out = sk._t_multi_edit({"edits": [_edit("a.py", "x = 1", "x = 99"),
                                      _edit("b.py", "v = 1", "v = 2",
                                            replace_all=True)]}, "/repo")
    assert out == {"ok": True, "files": ["a.py", "b.py"], "edits_applied": 2}
    assert (repo / "a.py").read_text().startswith("x = 99")


def test_a_failed_write_rolls_the_batch_back(repo, monkeypatch):
    """A half-written batch leaves the tree not compiling."""
    import pathlib
    real_write = pathlib.Path.write_text
    calls = {"n": 0}

    def _write(self, content, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        return real_write(self, content, **kw)
    monkeypatch.setattr(pathlib.Path, "write_text", _write)
    out = sk._t_multi_edit({"edits": [_edit("a.py", "x = 1", "x = 99"),
                                      _edit("b.py", "v = 1", "v = 2",
                                            replace_all=True)]}, "/repo")
    assert out["ok"] is False and "rolled back" in out["error"]
    assert (repo / "a.py").read_text() == "x = 1\ny = 2\n"


def test_a_rollback_that_also_fails_is_not_fatal(repo, monkeypatch):
    import pathlib
    monkeypatch.setattr(pathlib.Path, "write_text",
                        lambda self, content, **kw: (_ for _ in ()).throw(
                            OSError("read-only")))
    out = sk._t_multi_edit({"edits": [_edit("a.py", "x = 1", "x = 99")]}, "/repo")
    assert out["ok"] is False


def test_nothing_is_written_when_one_edit_is_invalid(repo):
    out = sk._t_multi_edit({"edits": [_edit("a.py", "x = 1", "x = 99"),
                                      _edit("a.py", "zzz", "y")]}, "/repo")
    assert out["ok"] is False
    assert (repo / "a.py").read_text() == "x = 1\ny = 2\n"


def test_a_batch_that_would_break_syntax_is_refused(repo, monkeypatch):
    monkeypatch.setattr(sk, "_syntax_check",
                        lambda ap, content, args: "unexpected indent")
    out = sk._t_multi_edit({"edits": [_edit("a.py", "x = 1", "  x = 99")]}, "/repo")
    assert out["error"] == "syntax_invalid" and out["file"] == "a.py"
    assert "force:true" in out["hint"]
    assert (repo / "a.py").read_text() == "x = 1\ny = 2\n"


@pytest.mark.parametrize("edits", [[], "not a list", None])
def test_an_empty_or_malformed_batch(repo, edits):
    assert sk._t_multi_edit({"edits": edits}, "/repo")["ok"] is False


# ─── the quality tools ─────────────────────────────────────────────────


def test_typecheck_delegates(monkeypatch):
    import aiforge_core.runtime.tools.typecheck as tc
    monkeypatch.setattr(tc, "typecheck", lambda: {"ok": True, "errors": 0})
    assert sk._t_typecheck({}, "/repo")["errors"] == 0


def test_format_defaults_to_the_whole_repo(monkeypatch):
    import aiforge_core.runtime.tools.format as fmt
    seen: dict = {}
    monkeypatch.setattr(fmt, "format",
                        lambda path: seen.setdefault("path", path) and {"ok": True})
    sk._t_format({}, "/repo")
    assert seen["path"] == "."


def test_the_language_server_query_is_forwarded(monkeypatch):
    import aiforge_core.runtime.tools.lsp as lsp_mod
    seen: dict = {}
    monkeypatch.setattr(lsp_mod, "lsp", lambda **kw: seen.update(kw) or {"ok": True})
    sk._t_lsp({"command": "definition", "path": "a.py", "line": "3",
               "character": None}, "/repo")
    assert seen == {"command": "definition", "path": "a.py", "line": 3,
                    "character": 0}


def test_the_test_runner_is_forwarded(monkeypatch):
    import aiforge_core.runtime.tools.test_runner as tr
    seen: dict = {}
    monkeypatch.setattr(tr, "run_tests",
                        lambda mode="fast", pattern="": seen.update(
                            mode=mode, pattern=pattern) or {"ok": True})
    sk._t_run_tests({"mode": "full", "pattern": "test_store"}, "/repo")
    assert seen == {"mode": "full", "pattern": "test_store"}
