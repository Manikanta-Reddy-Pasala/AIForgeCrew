"""L10 — pure-python delta detection: hash diff + git-diff parser."""
from __future__ import annotations

from unittest.mock import patch

from aiforge_memory.features.delta import extract as delta

# ─── hash diff ────────────────────────────────────────────────────────

def test_diff_hashes_classifies_changes() -> None:
    prev = {"a.py": "h1", "b.py": "h2", "c.py": "h3"}
    cur = {"a.py": "h1", "b.py": "CHANGED", "d.py": "h4"}
    added, modified, deleted = delta._diff_hashes(prev, cur)
    assert added == ["d.py"]
    assert modified == ["b.py"]
    assert deleted == ["c.py"]


def test_diff_hashes_empty_inputs() -> None:
    assert delta._diff_hashes({}, {}) == ([], [], [])


def test_diff_hashes_all_added() -> None:
    added, mod, deleted = delta._diff_hashes({}, {"x.py": "h"})
    assert added == ["x.py"]
    assert mod == [] and deleted == []


# ─── git diff parser ──────────────────────────────────────────────────

class _FakeProc:
    def __init__(self, stdout: bytes = b"", returncode: int = 0):
        self.stdout = stdout
        self.returncode = returncode


def test_git_diff_parses_added_modified_deleted(tmp_path) -> None:
    out = b"\0".join([
        b"A", b"new.py",
        b"M", b"changed.py",
        b"D", b"gone.py",
    ]) + b"\0"
    with patch("subprocess.run", return_value=_FakeProc(out)):
        a, m, d = delta._git_diff(tmp_path, "base", "head")
    assert a == ["new.py"]
    assert m == ["changed.py"]
    assert d == ["gone.py"]


def test_git_diff_parses_rename_as_delete_plus_add(tmp_path) -> None:
    out = b"\0".join([
        b"R100", b"old.py", b"new.py",
    ]) + b"\0"
    with patch("subprocess.run", return_value=_FakeProc(out)):
        a, m, d = delta._git_diff(tmp_path, "base", "head")
    assert "old.py" in d
    assert "new.py" in a


def test_git_diff_parses_type_change_as_modified(tmp_path) -> None:
    out = b"\0".join([b"T", b"link.py"]) + b"\0"
    with patch("subprocess.run", return_value=_FakeProc(out)):
        a, m, d = delta._git_diff(tmp_path, "base", "head")
    assert m == ["link.py"]


def test_git_diff_returns_empty_on_failure(tmp_path) -> None:
    with patch("subprocess.run", return_value=_FakeProc(b"", returncode=1)):
        a, m, d = delta._git_diff(tmp_path, "base", "head")
    assert a == [] and m == [] and d == []


# ─── changed_files cold-start ─────────────────────────────────────────

def test_changed_files_cold_start_returns_cold(tmp_path) -> None:
    from aiforge_memory.core import state as sdb
    conn = sdb.open_db(tmp_path / "s.db")
    sdb.migrate(conn)
    # No prior data and no git head — must report 'cold'.
    with patch.object(delta, "_git_head", return_value=None):
        cs = delta.changed_files(tmp_path, repo="x", state_conn=conn)
    assert cs.method == "cold"


def test_changed_files_uses_git_when_head_advances(tmp_path) -> None:
    from aiforge_memory.core import state as sdb
    conn = sdb.open_db(tmp_path / "s.db")
    sdb.migrate(conn)
    sdb.set_repo_git_head(conn, repo="x", head_sha="OLD", branch="main")

    with patch.object(delta, "_git_head", return_value=("NEW", "main")), \
         patch.object(delta, "_git_diff", return_value=(["a.py"], ["b.py"], [])):
        cs = delta.changed_files(tmp_path, repo="x", state_conn=conn)
    assert cs.method == "git"
    assert cs.head_sha == "NEW"
    assert cs.added == ["a.py"]
    assert cs.modified == ["b.py"]


def test_install_post_commit_hook_writes_executable(tmp_path) -> None:
    git_dir = tmp_path / ".git" / "hooks"
    git_dir.mkdir(parents=True)
    p = delta.install_post_commit_hook(tmp_path, "myrepo")
    assert p.exists()
    assert p.stat().st_mode & 0o111  # any exec bit set
    body = p.read_text()
    assert "aiforge-memory ingest" in body
    assert "--delta" in body


def test_install_post_commit_hook_raises_outside_git(tmp_path) -> None:
    import pytest
    with pytest.raises(FileNotFoundError):
        delta.install_post_commit_hook(tmp_path, "x")
