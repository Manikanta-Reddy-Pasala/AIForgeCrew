"""The startup migration chain, step by step.

Every step here runs on EVERY API boot against a real user's memory tree, so
the properties that matter are: idempotent (a second run changes nothing),
never-clobbering (a file already at the destination wins), and soft-fail (a
step that raises records the error and the boot continues). A migration that
breaks startup, or that undoes curation on a re-run, is worse than one that
never ran.
"""
from __future__ import annotations

import json

import pytest

from aiforge_core.memory import migrations as mg


@pytest.fixture
def mem(monkeypatch, tmp_path):
    """Point the whole memory tree at a temp dir."""
    from aiforge_core.memory import md_store
    root = tmp_path / "memory"
    root.mkdir()
    monkeypatch.setattr(md_store, "memory_dir", lambda: root)
    return root


def _md(path, frontmatter, body="body\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\n" + frontmatter + "\n---\n" + body)
    return path


# ─── archiving a stale okr/ DAG folder ─────────────────────────────────


def test_a_live_dag_folder_is_archived_not_deleted(mem):
    """Reversible: the folder moves to memory-archive/, it is never removed."""
    (mem / "okr").mkdir()
    (mem / "okr" / "node.md").write_text("x")
    out = mg._archive_okr_dag_folder()
    assert out["ok"] is True
    assert not (mem / "okr").exists()
    assert (mem.parent / "memory-archive" / "okr" / "node.md").read_text() == "x"


def test_a_second_archive_never_clobbers_the_first(mem):
    for _ in range(2):
        (mem / "okr").mkdir()
        (mem / "okr" / "node.md").write_text("x")
        mg._archive_okr_dag_folder()
    arch = mem.parent / "memory-archive"
    assert {p.name for p in arch.iterdir()} == {"okr", "okr-1"}


def test_no_dag_folder_is_a_no_op(mem):
    assert mg._archive_okr_dag_folder() == {"skipped": "no live okr/ folder"}


def test_a_marker_only_folder_is_not_archived_again(mem):
    """Otherwise every restart mints okr-1, okr-2, okr-3…"""
    (mem / "okr").mkdir()
    (mem / "okr" / mg._MIGRATIONS_JSON).write_text("{}")
    assert mg._archive_okr_dag_folder()["skipped"].startswith("only migration marker")
    assert (mem / "okr").exists()


def test_an_archive_failure_is_reported_not_raised(mem, monkeypatch):
    (mem / "okr").mkdir()
    (mem / "okr" / "node.md").write_text("x")
    import shutil
    monkeypatch.setattr(shutil, "move",
                        lambda *a: (_ for _ in ()).throw(OSError("read-only")))
    out = mg._archive_okr_dag_folder()
    assert out["ok"] is False
    assert "read-only" in out["error"]


# ─── frontmatter → OKF names ───────────────────────────────────────────


def test_legacy_keys_are_renamed(tmp_path):
    p = _md(tmp_path / "a.md", "kind: learning\nsource_url: http://x\nupdated_at: 2026")
    assert mg._rewrite_file_frontmatter_to_okf(p) is True
    text = p.read_text()
    assert "type: learning" in text
    assert "resource: http://x" in text
    assert "timestamp: 2026" in text
    assert "body" in text


def test_a_key_is_not_renamed_over_an_existing_okf_one(tmp_path):
    """Mixed files are safe: the OKF name already present wins."""
    p = _md(tmp_path / "a.md", "type: fact\nkind: learning")
    mg._rewrite_file_frontmatter_to_okf(p)
    assert p.read_text().count("type:") == 1
    assert "kind: learning" in p.read_text()


def test_a_second_pass_changes_nothing(tmp_path):
    p = _md(tmp_path / "a.md", "kind: learning")
    assert mg._rewrite_file_frontmatter_to_okf(p) is True
    assert mg._rewrite_file_frontmatter_to_okf(p) is False


def test_only_the_first_of_two_timestamp_sources_is_taken(tmp_path):
    p = _md(tmp_path / "a.md", "updated_at: 1\ncreated_at: 2")
    mg._rewrite_file_frontmatter_to_okf(p)
    assert p.read_text().count("timestamp:") == 1


def test_a_file_without_frontmatter_is_left_alone(tmp_path):
    p = tmp_path / "a.md"
    p.write_text("just prose\n")
    assert mg._rewrite_file_frontmatter_to_okf(p) is False


def test_an_unreadable_file_is_skipped(tmp_path):
    assert mg._rewrite_file_frontmatter_to_okf(tmp_path / "gone.md") is False


def test_an_unwritable_file_reports_no_change(tmp_path, monkeypatch):
    p = _md(tmp_path / "a.md", "kind: learning")
    monkeypatch.setattr(mg._atomic, "write_text",
                        lambda *a: (_ for _ in ()).throw(OSError("read-only")))
    assert mg._rewrite_file_frontmatter_to_okf(p) is False


def test_the_whole_tree_is_rewritten(mem):
    _md(mem / "compacted" / "a.md", "kind: learning")
    _md(mem / "captures" / "b.md", "kind: capture")
    out = mg._migrate_frontmatter_to_okf()
    assert out == {"ok": True, "rewritten": 2, "scanned": 2}


@pytest.mark.parametrize("rel", ["okf/index.md", "okf/log.md",
                                 "archive/old.md", "memory-archive/old.md"])
def test_reserved_and_archived_files_are_skipped(mem, rel):
    _md(mem / rel, "kind: learning")
    assert mg._migrate_frontmatter_to_okf()["scanned"] == 0


def test_a_missing_memory_dir_is_not_an_error(monkeypatch, tmp_path):
    from aiforge_core.memory import md_store
    monkeypatch.setattr(md_store, "memory_dir", lambda: tmp_path / "nope")
    assert mg._migrate_frontmatter_to_okf() == {"ok": True, "rewritten": 0, "scanned": 0}


def test_one_bad_file_does_not_stop_the_rest(mem, monkeypatch):
    _md(mem / "a.md", "kind: learning")
    _md(mem / "b.md", "kind: learning")
    calls = {"n": 0}

    def _rewrite(p):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("bad file")
        return True
    monkeypatch.setattr(mg, "_rewrite_file_frontmatter_to_okf", _rewrite)
    assert mg._migrate_frontmatter_to_okf()["rewritten"] == 1


# ─── okr/ → okf/ rename ────────────────────────────────────────────────


def test_a_legacy_bundle_is_renamed(mem):
    (mem / "okr").mkdir()
    (mem / "okr" / "n.md").write_text("x")
    assert mg._rename_okr_dir_to_okf()["ok"] is True
    assert (mem / "okf" / "n.md").exists()
    assert not (mem / "okr").exists()


def test_nothing_to_rename(mem):
    assert mg._rename_okr_dir_to_okf() == {"skipped": "no legacy okr/ folder"}


def test_a_stale_marker_only_okr_is_removed_when_okf_exists(mem):
    (mem / "okf").mkdir()
    (mem / "okr").mkdir()
    (mem / "okr" / mg._MIGRATIONS_JSON).write_text("{}")
    assert "removed_stale_marker" in mg._rename_okr_dir_to_okf()
    assert not (mem / "okr").exists()


def test_real_data_is_never_clobbered_by_the_rename(mem):
    (mem / "okf").mkdir()
    (mem / "okr").mkdir()
    (mem / "okr" / "n.md").write_text("x")
    assert mg._rename_okr_dir_to_okf()["skipped"].startswith("okf/ exists")
    assert (mem / "okr" / "n.md").exists()


def test_the_standalone_entry_runs_both_halves(mem, monkeypatch):
    monkeypatch.setattr(mg, "_rename_okr_dir_to_okf", lambda: {"ok": True})
    monkeypatch.setattr(mg, "_migrate_frontmatter_to_okf", lambda: {"ok": True})
    assert set(mg.migrate_okf_format()) == {"dir_rename", "frontmatter"}


# ─── foreign peers out of okf/ ─────────────────────────────────────────


class _Paths:
    def __init__(self, legacy, peers):
        self._legacy, self._peers = legacy, peers

    def legacy_peers_dir(self):
        return self._legacy

    def peers_root(self):
        return self._peers


@pytest.fixture
def peers(monkeypatch, tmp_path):
    from aiforge_core.memory.sync import paths as real
    legacy = tmp_path / "memory" / "okf" / "peers"
    inbox = tmp_path / "memory" / "peers"
    legacy.mkdir(parents=True)
    inbox.mkdir(parents=True)
    monkeypatch.setattr(real, "legacy_peers_dir", lambda: legacy)
    monkeypatch.setattr(real, "peers_root", lambda: inbox)
    return legacy, inbox


def test_foreign_nodes_move_to_the_inbox(peers):
    legacy, inbox = peers
    (legacy / "nuc").mkdir()
    (legacy / "nuc" / "n.md").write_text("x")
    out = mg._move_okf_peers_to_inbox()
    assert out == {"ok": True, "moved": 1, "kept_at_destination": 0}
    assert (inbox / "nuc" / "n.md").read_text() == "x"
    assert not legacy.exists()          # fully drained, so it is gone


def test_a_file_already_at_the_destination_is_kept(peers):
    legacy, inbox = peers
    (legacy / "nuc").mkdir()
    (legacy / "nuc" / "n.md").write_text("old")
    (inbox / "nuc").mkdir()
    (inbox / "nuc" / "n.md").write_text("newer")
    out = mg._move_okf_peers_to_inbox()
    assert out["kept_at_destination"] == 1
    assert out["moved"] == 0
    assert (inbox / "nuc" / "n.md").read_text() == "newer"


def test_no_legacy_peers_folder(peers, monkeypatch):
    from aiforge_core.memory.sync import paths as real
    monkeypatch.setattr(real, "legacy_peers_dir", lambda: peers[0] / "gone")
    assert mg._move_okf_peers_to_inbox()["skipped"] == "no legacy okf/peers/ folder"


def test_a_peers_migration_failure_never_blocks_boot(monkeypatch):
    from aiforge_core.memory.sync import paths as real

    def _boom():
        raise RuntimeError("tree is odd")
    monkeypatch.setattr(real, "legacy_peers_dir", _boom)
    assert mg._move_okf_peers_to_inbox()["ok"] is False


def test_rmdir_if_empty_tolerates_a_full_or_missing_dir(tmp_path):
    full = tmp_path / "full"
    full.mkdir()
    (full / "f").write_text("x")
    mg._rmdir_if_empty(full)
    mg._rmdir_if_empty(tmp_path / "gone")
    assert full.exists()


# ─── repo discovery ────────────────────────────────────────────────────


def test_repos_are_discovered_from_the_configured_root(monkeypatch, tmp_path):
    for name in ("alpha", "beta"):
        (tmp_path / name / ".git").mkdir(parents=True)
    (tmp_path / "not-a-repo").mkdir()
    monkeypatch.setenv("AIFORGE_REPOS_ROOT", str(tmp_path))
    import subprocess
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no git")))
    assert mg._discover_repos() == ["alpha", "beta"]


def test_an_unreadable_root_is_skipped(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_REPOS_ROOT", str(tmp_path / "gone"))
    import subprocess
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no git")))
    assert mg._discover_repos() == []


def test_sibling_repos_of_the_checkout_are_discovered(monkeypatch, tmp_path):
    (tmp_path / "sibling" / ".git").mkdir(parents=True)
    monkeypatch.delenv("AIFORGE_REPOS_ROOT", raising=False)
    import subprocess

    class _R:
        returncode = 0
        stdout = str(tmp_path / "self") + "\n"
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _R())
    assert mg._discover_repos() == ["sibling"]


# ─── the one-shot marker ───────────────────────────────────────────────


@pytest.fixture
def marker(monkeypatch, tmp_path):
    from aiforge_core.memory.okf import store
    root = tmp_path / "okf"
    root.mkdir()
    monkeypatch.setattr(store, "okf_root", lambda: str(root))
    return root


def test_the_marker_round_trips(marker):
    mg._save_marker({"done": ["classify"], "version": 1})
    assert mg._load_marker() == {"done": ["classify"], "version": 1}


def test_a_missing_marker_reads_as_empty(marker):
    assert mg._load_marker() == {}


def test_a_corrupt_marker_reads_as_empty(marker):
    (marker / mg._MIGRATIONS_JSON).write_text("{not json")
    assert mg._load_marker() == {}


def test_a_marker_that_is_not_an_object_reads_as_empty(marker):
    (marker / mg._MIGRATIONS_JSON).write_text('["a"]')
    assert mg._load_marker() == {}


def test_an_unwritable_marker_is_not_fatal(marker, monkeypatch):
    monkeypatch.setattr(mg._atomic, "write_text",
                        lambda *a: (_ for _ in ()).throw(OSError("read-only")))
    mg._save_marker({"done": []})


# ─── code-vs-prose classification ──────────────────────────────────────


@pytest.mark.parametrize("body", [
    "def f():\n    return 1\nimport os\n",
    "public static void main(String[] a) { return; }",
    "const x = 1;\nlet y => 2;\nself.z = 3",
    "import os",
])
def test_source_code_is_recognised(body):
    assert mg._body_looks_like_code(body) is True


@pytest.mark.parametrize("body", [
    "The bank statement parser reads OCR output when the PDF has no text layer.",
    "Prefer squash merges on this repo.",
    "",
])
def test_prose_is_kept(body):
    assert mg._body_looks_like_code(body) is False


def test_a_long_prose_body_mentioning_one_keyword_is_still_prose():
    body = ("We decided to import the ledger monthly because the vendor's feed "
            "is only refreshed at month end, and reconciling daily was noise. " * 3)
    assert mg._body_looks_like_code(body) is False


# ─── purging a bad drain ───────────────────────────────────────────────


class _MdStore:
    def __init__(self, root, docs):
        self._root, self._docs = root, docs

    def memory_dir(self):
        return self._root

    def _parse(self, p):
        return self._docs[p.name]


def test_only_files_stamped_by_the_bad_drain_are_removed(tmp_path):
    (tmp_path / "a.md").write_text("x")
    (tmp_path / "b.md").write_text("x")
    store = _MdStore(tmp_path, {"a.md": {"source": "migrate:neo4j"},
                                "b.md": {"source": "capture"}})
    assert mg._purge_drained_md(store) == 1
    assert not (tmp_path / "a.md").exists()
    assert (tmp_path / "b.md").exists()


def test_an_unparseable_file_is_left_alone(tmp_path):
    (tmp_path / "a.md").write_text("x")

    class _Bad(_MdStore):
        def _parse(self, p):
            raise ValueError("bad frontmatter")
    assert mg._purge_drained_md(_Bad(tmp_path, {})) == 0
    assert (tmp_path / "a.md").exists()


class _OkrStore:
    def __init__(self, docs):
        self._docs = docs

    def load_all(self):
        return self._docs


def test_code_learnings_are_removed_and_prose_kept(tmp_path):
    code = tmp_path / "code.md"
    prose = tmp_path / "prose.md"
    code.write_text("x")
    prose.write_text("x")
    out = {"removed_okr_learnings": 0, "kept_learnings": 0}
    mg._purge_code_learnings(_OkrStore([
        {"type": "learning", "body": "def f():\n import os\n class C: pass",
         "path": str(code)},
        {"type": "learning", "body": "Prefer squash merges.", "path": str(prose)},
        {"type": "solution", "body": "def f(): pass", "path": str(code)},
    ]), out)
    assert out == {"removed_okr_learnings": 1, "kept_learnings": 1}
    assert not code.exists()
    assert prose.exists()


def test_a_learning_whose_file_is_gone_is_not_counted(tmp_path):
    out = {"removed_okr_learnings": 0, "kept_learnings": 0}
    mg._purge_code_learnings(_OkrStore([
        {"type": "learning", "body": "import os\ndef f(): pass\nclass C: pass",
         "path": str(tmp_path / "gone.md")}]), out)
    assert out["removed_okr_learnings"] == 0


def test_the_purge_rebuilds_the_briefs_and_index(monkeypatch, tmp_path):
    from aiforge_core.memory import md_store
    from aiforge_core.memory.okf import store as okr_store
    monkeypatch.setattr(mg, "_purge_drained_md", lambda s: 2)
    monkeypatch.setattr(mg, "_purge_code_learnings", lambda s, out: None)
    called: list = []
    monkeypatch.setattr(md_store, "compact", lambda **kw: called.append("compact"))
    monkeypatch.setattr(md_store, "sweep_stale_captures", lambda **kw: called.append("sweep"))
    monkeypatch.setattr(okr_store, "_invalidate", lambda: called.append("invalidate"))
    monkeypatch.setattr(okr_store, "_write_index", lambda: called.append("index"))
    out = mg.purge_migrated_code()
    assert out["ok"] is True
    assert out["removed_md"] == 2
    assert called == ["compact", "sweep", "invalidate", "index"]


def test_a_rebuild_failure_does_not_undo_the_purge(monkeypatch):
    from aiforge_core.memory import md_store
    monkeypatch.setattr(mg, "_purge_drained_md", lambda s: 1)
    monkeypatch.setattr(mg, "_purge_code_learnings", lambda s, out: None)
    monkeypatch.setattr(md_store, "compact",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("no llm")))
    assert mg.purge_migrated_code()["ok"] is True


# ─── the step wrapper ──────────────────────────────────────────────────


def test_a_step_records_its_result():
    out: dict = {}
    assert mg._step(out, "s", lambda: {"ok": True, "n": 1}) is True
    assert out["s"] == {"ok": True, "n": 1}


def test_a_step_that_raises_is_recorded_as_not_run():
    out: dict = {}

    def _boom():
        raise RuntimeError("bad")
    assert mg._step(out, "s", _boom) is False
    assert out["s"] == {"ok": False, "error": "bad"}


def test_a_step_reporting_failure_still_counts_as_having_run():
    out: dict = {}
    assert mg._step(out, "s", lambda: {"ok": False}) is True


# ─── boot-time compaction window ───────────────────────────────────────


@pytest.fixture
def compact(monkeypatch):
    from aiforge_core.memory import md_store
    from aiforge_core.runtime import compact_window
    seen: dict = {"summarize": []}

    def _compact(group_by, min_group=None, summarize=False, model_role=None,
                 archive_sources=False, **kw):
        seen["summarize"].append(summarize)
        return {"files_in": 3}
    monkeypatch.setattr(md_store, "compact", _compact)
    monkeypatch.setattr(md_store, "sweep_stale_captures", lambda **kw: {"swept": 1})
    monkeypatch.setattr(compact_window, "disabled", lambda: False)
    monkeypatch.setattr(compact_window, "open_now", lambda: False)
    monkeypatch.setattr(compact_window, "at_hour", lambda: 18)
    return seen


def test_outside_the_window_the_boot_fold_leaves_the_model_out(compact, monkeypatch):
    """One LLM call per brief on every boot is the "compaction in my working
    day" intrusion the evening window exists to remove."""
    monkeypatch.setenv("AIFORGE_STARTUP_COMPACT", "window")
    out: dict = {}
    mg._startup_compact(out)
    assert compact["summarize"] == [False, False]
    assert out["compact"] == {"repo_in": 3, "topic_in": 3, "swept": 1,
                              "summarized": False}


def test_inside_the_window_the_learner_runs(compact, monkeypatch):
    from aiforge_core.runtime import compact_window
    monkeypatch.setenv("AIFORGE_STARTUP_COMPACT", "window")
    monkeypatch.setattr(compact_window, "open_now", lambda: True)
    out: dict = {}
    mg._startup_compact(out)
    assert compact["summarize"] == [True, True]


def test_always_restores_the_old_boot_time_fold(compact, monkeypatch):
    monkeypatch.setenv("AIFORGE_STARTUP_COMPACT", "always")
    out: dict = {}
    mg._startup_compact(out)
    assert compact["summarize"] == [True, True]


def test_compaction_turned_off_suppresses_only_the_llm(compact, monkeypatch):
    from aiforge_core.runtime import compact_window
    monkeypatch.setenv("AIFORGE_STARTUP_COMPACT", "always")
    monkeypatch.setattr(compact_window, "disabled", lambda: True)
    out: dict = {}
    mg._startup_compact(out)
    assert compact["summarize"] == [False, False]
    assert out["compact"]["repo_in"] == 3      # the structural fold still ran


@pytest.mark.parametrize("mode", ["off", "0", "false", "no"])
def test_the_boot_fold_can_be_skipped_entirely(monkeypatch, mode):
    monkeypatch.setenv("AIFORGE_STARTUP_COMPACT", mode)
    from aiforge_core.memory import md_store
    monkeypatch.setattr(md_store, "compact",
                        lambda **kw: pytest.fail("compacted with the gate off"))
    out: dict = {}
    mg._startup_compact(out)
    assert out["compact"] == {"skipped": "disabled"}


def test_a_compaction_crash_never_blocks_boot(monkeypatch):
    monkeypatch.setenv("AIFORGE_STARTUP_COMPACT", "always")
    from aiforge_core.memory import md_store
    monkeypatch.setattr(md_store, "compact",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("no llm")))
    out: dict = {}
    mg._startup_compact(out)
    assert out["compact"]["ok"] is False


# ─── re-embedding ──────────────────────────────────────────────────────


def test_a_changed_embedder_triggers_a_reembed(monkeypatch):
    from aiforge_core.memory import backend_select, sqlite_memory
    monkeypatch.setattr(backend_select, "embedded", lambda: True)
    monkeypatch.setattr(sqlite_memory, "stored_dim_mismatch", lambda: True)
    monkeypatch.setattr(sqlite_memory, "reembed_all", lambda: {"ok": True, "n": 12})
    out: dict = {}
    mg._reembed_if_embedder_changed(out)
    assert out["reembed"] == {"ok": True, "n": 12}


def test_matching_dims_are_left_alone(monkeypatch):
    from aiforge_core.memory import backend_select, sqlite_memory
    monkeypatch.setattr(backend_select, "embedded", lambda: True)
    monkeypatch.setattr(sqlite_memory, "stored_dim_mismatch", lambda: False)
    monkeypatch.setattr(sqlite_memory, "stored_embedder_changed", lambda: False)
    monkeypatch.setattr(sqlite_memory, "reembed_all",
                        lambda: pytest.fail("re-embedded a matching store"))
    out: dict = {}
    mg._reembed_if_embedder_changed(out)
    assert out == {}


def test_a_non_embedded_backend_is_skipped(monkeypatch):
    from aiforge_core.memory import backend_select
    monkeypatch.setattr(backend_select, "embedded", lambda: False)
    out: dict = {}
    mg._reembed_if_embedder_changed(out)
    assert out == {}


def test_a_reembed_failure_is_recorded(monkeypatch):
    from aiforge_core.memory import backend_select
    monkeypatch.setattr(backend_select, "embedded",
                        lambda: (_ for _ in ()).throw(RuntimeError("db locked")))
    out: dict = {}
    mg._reembed_if_embedder_changed(out)
    assert out["reembed"]["ok"] is False


# ─── folder moves ──────────────────────────────────────────────────────


def test_legacy_files_are_moved_into_their_folders(monkeypatch):
    from aiforge_core.memory import md_store
    monkeypatch.setattr(md_store, "migrate_briefs_to_folder", lambda: {"moved": 2})
    monkeypatch.setattr(md_store, "migrate_captures_to_folder", lambda: {"moved": 5})
    out: dict = {}
    mg._move_files_into_folders(out)
    assert out == {"briefs_folder": {"moved": 2}, "captures_folder": {"moved": 5}}


def test_a_move_failure_is_recorded(monkeypatch):
    from aiforge_core.memory import md_store
    monkeypatch.setattr(md_store, "migrate_briefs_to_folder",
                        lambda: (_ for _ in ()).throw(OSError("read-only")))
    out: dict = {}
    mg._move_files_into_folders(out)
    assert out["briefs_folder"]["ok"] is False


# ─── the chain ─────────────────────────────────────────────────────────


@pytest.fixture
def chain(monkeypatch, marker):
    for name in ("_reembed_if_embedder_changed", "_move_files_into_folders",
                 "_startup_compact"):
        monkeypatch.setattr(mg, name, lambda out, _n=name: out.update({_n: {"ok": True}}))
    monkeypatch.setattr(mg, "_migrate_md_format", lambda: {"ok": True})
    monkeypatch.setattr(mg, "_migrate_frontmatter_to_okf", lambda: {"ok": True})
    monkeypatch.setattr(mg, "_archive_okr_dag_folder", lambda: {"ok": True})
    monkeypatch.setattr(mg, "_move_okf_peers_to_inbox", lambda: {"ok": True})
    monkeypatch.delenv("AIFORGE_OKR_DAG", raising=False)
    return marker


def test_the_default_chain_archives_the_dag_instead_of_building_it(chain, monkeypatch):
    monkeypatch.setattr(mg, "_dag_steps",
                        lambda out, done: pytest.fail("ran DAG steps with the flag off"))
    out = mg.run_startup_migrations()
    assert out["okr_archive"] == {"ok": True}
    assert out["format"] == {"ok": True}
    assert out["okf_frontmatter"] == {"ok": True}


def test_a_one_shot_step_is_not_repeated(chain):
    mg.run_startup_migrations()
    assert json.loads((chain / mg._MIGRATIONS_JSON).read_text())["done"] == \
        ["peers_out_of_okf"]
    # second boot: the step is marked done, so it must not run again
    import aiforge_core.memory.migrations as mod
    mod._move_okf_peers_to_inbox = lambda: pytest.fail("re-ran a one-shot step")
    try:
        assert "peers_out_of_okf" not in mg.run_startup_migrations()
    finally:
        mod._move_okf_peers_to_inbox = lambda: {"ok": True}


def test_a_failed_one_shot_step_is_retried_next_boot(chain, monkeypatch):
    monkeypatch.setattr(mg, "_move_okf_peers_to_inbox", lambda: {"ok": False})
    mg.run_startup_migrations()
    assert json.loads((chain / mg._MIGRATIONS_JSON).read_text())["done"] == []


def test_the_dag_branch_runs_when_enabled(chain, monkeypatch):
    monkeypatch.setenv("AIFORGE_OKR_DAG", "1")
    monkeypatch.setattr(mg, "_dag_steps", lambda out, done: out.update({"dag_ran": True}))
    monkeypatch.setattr(mg, "_archive_okr_dag_folder",
                        lambda: pytest.fail("archived the DAG while it is enabled"))
    assert mg.run_startup_migrations()["dag_ran"] is True


def test_a_crash_in_the_dag_branch_never_blocks_boot(chain, monkeypatch):
    monkeypatch.setenv("AIFORGE_OKR_DAG", "1")

    def _boom(out, done):
        raise RuntimeError("okf store broken")
    monkeypatch.setattr(mg, "_dag_steps", _boom)
    assert mg.run_startup_migrations()["dag"]["ok"] is False


# ─── dedupe ────────────────────────────────────────────────────────────


def test_dedupe_covers_chat_sessions(monkeypatch):
    from aiforge_core.runtime import chat_store
    monkeypatch.delenv("AIFORGE_OKR_DAG", raising=False)
    monkeypatch.setattr(chat_store, "dedupe_sessions", lambda: {"removed": 3})
    assert mg.dedupe_all() == {"chat": {"removed": 3}}


def test_dedupe_covers_okr_nodes_when_the_dag_is_on(monkeypatch):
    from aiforge_core.memory.okf import store
    from aiforge_core.runtime import chat_store
    monkeypatch.setenv("AIFORGE_OKR_DAG", "1")
    monkeypatch.setattr(store, "dedupe_nodes", lambda: {"removed": 1})
    monkeypatch.setattr(chat_store, "dedupe_sessions", lambda: {"removed": 0})
    assert mg.dedupe_all()["okr"] == {"removed": 1}


def test_each_side_of_dedupe_fails_softly(monkeypatch):
    from aiforge_core.runtime import chat_store
    monkeypatch.delenv("AIFORGE_OKR_DAG", raising=False)
    monkeypatch.setattr(chat_store, "dedupe_sessions",
                        lambda: (_ for _ in ()).throw(RuntimeError("db locked")))
    assert mg.dedupe_all()["chat"]["ok"] is False


# ─── the on-demand full recompact ──────────────────────────────────────


def test_progress_is_reported_around_each_step():
    seen: list = []
    out: dict = {}
    mg._run_recompact_step(1, 3, "topic", lambda: {"ok": True}, out,
                           lambda name, phase, result: seen.append((name, phase)))
    assert seen == [("topic", "run"), ("topic", "done")]
    assert out["topic"] == {"ok": True}


def test_a_crashed_step_is_recorded_and_the_rest_continue():
    out: dict = {}
    mg._run_recompact_step(1, 3, "topic",
                           lambda: (_ for _ in ()).throw(RuntimeError("no llm")),
                           out, None)
    assert out["topic"] == {"ok": False, "error": "no llm"}


def test_a_reporting_error_never_breaks_the_recompact():
    def _boom(*_a):
        raise RuntimeError("ui gone")
    mg._notify_step(_boom, "topic", "run", None)
    mg._notify_step(None, "topic", "run", None)


def test_every_recompact_step_runs(monkeypatch):
    ran: list = []
    monkeypatch.setattr(mg, "_run_recompact_step",
                        lambda i, total, name, fn, out, on_step: ran.append(name))
    out = mg.force_recompact_all()
    assert out["ok"] is True
    assert ran[:3] == ["tidy_legacy", "repo", "topic"]
    assert "reingest" in ran
    assert "map_scopes" in ran
