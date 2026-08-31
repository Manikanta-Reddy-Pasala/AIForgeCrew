"""aiforge-maint: the operator CLI. Was at 0% coverage.

Each subcommand is a thin wrapper whose contract is (a) it prints ONE json
line and (b) its exit code is honest — a shell chaining `... && next-step`
reads it. The repo-notes command previously printed {"error": ...} and exited
0, so a failure read as success; that is pinned here.
"""
from __future__ import annotations

import json
import os

import pytest

from aiforge_core.cli import maintenance as mnt


def _run(monkeypatch, capsys, argv):
    rc = mnt.main(argv)
    return rc, capsys.readouterr().out.strip()


# ── runtime.env sourcing ──────────────────────────────────────────────


def test_runtime_env_is_sourced_without_clobbering_explicit_env(tmp_path, monkeypatch):
    f = tmp_path / "runtime.env"
    f.write_text('# a comment\n\nFROM_FILE="file-value"\nALREADY_SET=from-file\n')
    monkeypatch.setenv("AIFORGE_RUNTIME_ENV", str(f))
    monkeypatch.setenv("ALREADY_SET", "from-shell")
    monkeypatch.delenv("FROM_FILE", raising=False)

    mnt._load_runtime_env()
    assert os.environ["FROM_FILE"] == "file-value", "quotes are stripped"
    assert os.environ["ALREADY_SET"] == "from-shell", \
        "an explicit export must stay authoritative"


def test_runtime_env_missing_file_is_a_no_op(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_RUNTIME_ENV", str(tmp_path / "nope.env"))
    mnt._load_runtime_env()   # must not raise


# ── subcommands ───────────────────────────────────────────────────────


def test_memory_decay_prints_one_json_line(monkeypatch, capsys):
    monkeypatch.setattr("aiforge_core.memory.decay.run", lambda: {"removed": 3})
    rc, out = _run(monkeypatch, capsys, ["memory", "decay"])
    assert rc == 0
    assert json.loads(out) == {"cmd": "memory.decay", "removed": 3}


def test_memory_reembed_prints_one_json_line(monkeypatch, capsys):
    monkeypatch.setattr("aiforge_core.memory.sqlite_memory.reembed_all",
                        lambda: {"updated": 7})
    rc, out = _run(monkeypatch, capsys, ["memory", "reembed"])
    assert rc == 0
    assert json.loads(out) == {"cmd": "memory.reembed", "updated": 7}


def test_memory_migrate_okr_prints_the_result(monkeypatch, capsys):
    monkeypatch.setattr("aiforge_core.memory.md_store.migrate_to_okr",
                        lambda: {"moved": 2})
    rc, out = _run(monkeypatch, capsys, ["memory", "migrate-okr"])
    assert rc == 0
    assert json.loads(out) == {"moved": 2}


def test_index_merkle_reports_the_root(monkeypatch, capsys):
    monkeypatch.setattr("aiforge_core.indexing.merkle.build", lambda p: "deadbeef")
    rc, out = _run(monkeypatch, capsys, ["index", "merkle", "/some/path"])
    assert rc == 0
    assert json.loads(out) == {"cmd": "index.merkle", "path": "/some/path",
                               "root": "deadbeef"}


def test_docs_ingest_passes_the_chunk_size_through(monkeypatch, capsys):
    seen = {}

    def _ingest(library, urls, *, chunk_chars):
        seen.update(library=library, urls=urls, chunk_chars=chunk_chars)
        return 4
    monkeypatch.setattr("aiforge_core.indexing.docs_index.ingest", _ingest)
    rc, out = _run(monkeypatch, capsys,
                   ["docs", "ingest", "spring", "http://a", "http://b",
                    "--chunk-chars", "900"])
    assert rc == 0
    assert json.loads(out)["added"] == 4
    assert seen["library"] == "spring"
    assert seen["urls"] == ["http://a", "http://b"]
    assert seen["chunk_chars"] == 900


def test_cost_snapshot_prints_the_snapshot(monkeypatch, capsys):
    """Regression: this imported `aiforge_core.runtime.cost`, which does not
    exist — the module is under observability. main()'s catch-all printed the
    ImportError and returned 0, so the command was dead and looked fine."""
    monkeypatch.setattr("aiforge_core.observability.cost.snapshot",
                        lambda t: {"ticket": t, "usd": 1.5})
    rc, out = _run(monkeypatch, capsys, ["cost", "snapshot", "--ticket", "ONE-1"])
    assert rc == 0
    assert json.loads(out) == {"ticket": "ONE-1", "usd": 1.5}


# ── the exit-code contract that actually bit ──────────────────────────


def test_repo_notes_exits_zero_and_reports_the_path_on_success(monkeypatch, capsys):
    monkeypatch.setattr(
        "aiforge_core.indexing.repo_notes.generate_repo_notes",
        lambda repo: "/tmp/notes.md")
    rc, out = _run(monkeypatch, capsys, ["repo", "notes", "myrepo"])
    assert rc == 0
    assert json.loads(out) == {"repo": "myrepo", "wrote": "/tmp/notes.md"}


def test_repo_notes_exits_NON_zero_when_generation_fails(monkeypatch, capsys):
    """The regression this guards: it printed {"error": ...} and returned 0, so
    `aiforge-maint repo notes X && next-step` ran next-step on a failure."""
    def _boom(repo):
        raise RuntimeError("no such repo")
    monkeypatch.setattr(
        "aiforge_core.indexing.repo_notes.generate_repo_notes", _boom)
    rc, out = _run(monkeypatch, capsys, ["repo", "notes", "myrepo"])
    assert rc == 1, "a failure must not be readable as success by a shell"
    assert "no such repo" in json.loads(out)["error"]


def test_cost_snapshot_imports_the_module_that_actually_exists():
    """Guards the import path directly, since main() swallows an ImportError
    into a 0 exit code and this would otherwise stay invisible."""
    import importlib
    assert importlib.util.find_spec("aiforge_core.observability.cost")
    assert importlib.util.find_spec("aiforge_core.runtime.cost") is None


def test_unknown_command_is_rejected(monkeypatch):
    with pytest.raises(SystemExit):
        mnt.main(["not-a-command"])
