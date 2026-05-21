from __future__ import annotations

import pytest

from aiforge_core.runtime.tools import editor as ed


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_REPO_ROOT", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(parents=True, exist_ok=True)
    (tmp_path / "hello.py").write_text("print('hi')\n", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "nested.txt").write_text(
        "a\nb\nc\nd\ne\n", encoding="utf-8"
    )
    return tmp_path


# ─── view ───────────────────────────────────────────────────────────────


def test_view_returns_full_file_content(repo):
    out = ed.editor("view", "hello.py")
    assert out["ok"]
    assert out["content"] == "print('hi')\n"
    assert out["total_lines"] == 1


def test_view_with_range_returns_slice(repo):
    out = ed.editor("view", "sub/nested.txt", view_range=[2, 4])
    assert out["ok"]
    assert out["content"] == "b\nc\nd\n"


def test_view_dir_returns_tree(repo):
    out = ed.editor("view", "")
    assert out["ok"]
    names = {entry["name"] for entry in out["entries"]}
    assert "hello.py" in names
    assert "sub" in names


def test_view_missing_file_returns_error(repo):
    out = ed.editor("view", "nope.py")
    assert out["ok"] is False
    assert out["error"] == "not_found"


def test_view_path_traversal_rejected(repo):
    out = ed.editor("view", "../etc/passwd")
    assert out["ok"] is False
    assert out["error"] == "path_traversal"


def test_view_invalid_range(repo):
    out = ed.editor("view", "sub/nested.txt", view_range=[5, 2])
    assert out["ok"] is False
    assert out["error"] == "invalid_view_range"


# ─── create ─────────────────────────────────────────────────────────────


def test_create_happy(repo):
    out = ed.editor("create", "new.txt", file_text="hello\n")
    assert out["ok"]
    assert (repo / "new.txt").read_text() == "hello\n"
    assert out["bytes"] == 6


def test_create_rejects_existing(repo):
    out = ed.editor("create", "hello.py", file_text="x = 1\n")
    assert out["ok"] is False
    assert out["error"] == "exists"


def test_create_rejects_bad_python_syntax(repo, monkeypatch):
    monkeypatch.delenv("AIFORGE_DOER_SKIP_SYNTAX", raising=False)
    out = ed.editor("create", "bad.py", file_text="def foo(:\n")
    assert out["ok"] is False
    assert out["error"].startswith("syntax_invalid")


def test_create_missing_text(repo):
    out = ed.editor("create", "x.txt")
    assert out["ok"] is False
    assert out["error"] == "missing_file_text"


def test_create_pushes_undo_snapshot(repo):
    out = ed.editor("create", "snap.txt", file_text="v1\n")
    assert out["ok"]
    from aiforge_core.runtime.tools.editor import _undo_dir_for
    snap_dir = _undo_dir_for(repo / "snap.txt")
    snaps = list(snap_dir.glob("*.txt"))
    assert len(snaps) == 1
    assert snaps[0].read_text() == ""


# ─── str_replace ────────────────────────────────────────────────────────


def test_str_replace_happy(repo):
    (repo / "doc.md").write_text("hello world\n", encoding="utf-8")
    out = ed.editor(
        "str_replace", "doc.md", old_str="world", new_str="earth",
    )
    assert out["ok"]
    assert (repo / "doc.md").read_text() == "hello earth\n"


def test_str_replace_not_found_when_missing(repo):
    out = ed.editor("str_replace", "missing.md", old_str="x", new_str="y")
    assert out["ok"] is False
    assert out["error"] == "not_found"


def test_str_replace_old_text_not_found(repo):
    (repo / "doc.md").write_text("hello\n", encoding="utf-8")
    out = ed.editor("str_replace", "doc.md", old_str="nope", new_str="y")
    assert out["ok"] is False
    assert out["error"] == "old_text_not_found"


def test_str_replace_ambiguous_match(repo):
    (repo / "doc.md").write_text("a\na\n", encoding="utf-8")
    out = ed.editor("str_replace", "doc.md", old_str="a", new_str="b")
    assert out["ok"] is False
    assert out["error"] == "ambiguous_match"
    assert out["occurrences"] == 2


def test_str_replace_pushes_snapshot(repo):
    (repo / "doc.md").write_text("hello world\n", encoding="utf-8")
    ed.editor("str_replace", "doc.md", old_str="world", new_str="earth")
    from aiforge_core.runtime.tools.editor import _undo_dir_for
    snaps = list(_undo_dir_for(repo / "doc.md").glob("*.txt"))
    assert len(snaps) == 1
    assert snaps[0].read_text() == "hello world\n"


# ─── insert ─────────────────────────────────────────────────────────────


def test_insert_at_top(repo):
    (repo / "doc.md").write_text("a\nb\n", encoding="utf-8")
    out = ed.editor("insert", "doc.md", insert_line=0, new_str="x\n")
    assert out["ok"]
    assert (repo / "doc.md").read_text() == "x\na\nb\n"
    assert out["inserted_at"] == 0


def test_insert_mid_file(repo):
    (repo / "doc.md").write_text("a\nb\nc\n", encoding="utf-8")
    out = ed.editor("insert", "doc.md", insert_line=2, new_str="X\n")
    assert out["ok"]
    assert (repo / "doc.md").read_text() == "a\nb\nX\nc\n"


def test_insert_beyond_eof_rejects(repo):
    (repo / "doc.md").write_text("a\n", encoding="utf-8")
    out = ed.editor("insert", "doc.md", insert_line=99, new_str="X\n")
    assert out["ok"] is False
    assert out["error"] == "line_out_of_range"


def test_insert_missing_file(repo):
    out = ed.editor("insert", "missing.md", insert_line=0, new_str="X\n")
    assert out["ok"] is False
    assert out["error"] == "not_found"


# ─── undo_edit ──────────────────────────────────────────────────────────


def test_undo_edit_restores_previous_version(repo):
    (repo / "doc.md").write_text("v1\n", encoding="utf-8")
    ed.editor("str_replace", "doc.md", old_str="v1", new_str="v2")
    assert (repo / "doc.md").read_text() == "v2\n"
    out = ed.editor("undo_edit", "doc.md")
    assert out["ok"]
    assert (repo / "doc.md").read_text() == "v1\n"


def test_undo_edit_no_history(repo):
    (repo / "fresh.md").write_text("untouched\n", encoding="utf-8")
    out = ed.editor("undo_edit", "fresh.md")
    assert out["ok"] is False
    assert out["error"] == "no_history"


def test_undo_edit_ring_walks_history(repo):
    (repo / "doc.md").write_text("v1\n", encoding="utf-8")
    ed.editor("str_replace", "doc.md", old_str="v1", new_str="v2")
    ed.editor("str_replace", "doc.md", old_str="v2", new_str="v3")
    assert (repo / "doc.md").read_text() == "v3\n"
    ed.editor("undo_edit", "doc.md")
    assert (repo / "doc.md").read_text() == "v2\n"
    ed.editor("undo_edit", "doc.md")
    assert (repo / "doc.md").read_text() == "v1\n"


# ─── sub-command allowlist ──────────────────────────────────────────────


def test_non_doer_blocked_from_mutating_commands(repo, monkeypatch):
    monkeypatch.setattr(
        ed, "_load_editor_commands_for_role",
        lambda role: ["view"] if role == "planner" else None,
    )
    out = ed.editor("create", "x.txt", file_text="y", _agent_role="planner")
    assert out["ok"] is False
    assert out["error"] == "editor_command_not_allowed"


def test_doer_allowed_all_commands(repo, monkeypatch):
    monkeypatch.setattr(
        ed, "_load_editor_commands_for_role",
        lambda role: None,
    )
    out = ed.editor("create", "y.txt", file_text="z\n", _agent_role="doer")
    assert out["ok"]


# ─── unknown command ────────────────────────────────────────────────────


def test_unknown_command(repo):
    out = ed.editor("delete", "hello.py")
    assert out["ok"] is False
    assert out["error"] == "unknown_command"
