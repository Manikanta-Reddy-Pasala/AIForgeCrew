"""Chat projects: the folders under the repos root and each one's memory.

A project's brief lives in the memory store and is mirrored, both ways, to
``<repo>/.aiforge/memory/MEMORY.md``. These tests pin the parts that would
hurt if they slipped: a hand edit in the repo is imported (and a fact the
store learned meanwhile is kept), nothing outside ``.aiforge/`` is written, a
read-only repo degrades instead of failing, stale facts are only the ones
whose files are really gone, and forgetting archives rather than destroys.
"""
from __future__ import annotations

import os

import pytest


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "cfg" / "memory"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "cfg" / "m.db"))
    monkeypatch.setenv("AIFORGE_PROJECT_INGEST", "0")
    root = tmp_path / "repos"
    (root / "shop").mkdir(parents=True)
    (root / "billing").mkdir()
    (root / ".hidden").mkdir()
    (root / "notes.txt").write_text("x")
    monkeypatch.setenv("AIFORGE_PROJECTS_ROOT", str(root))
    from aiforge_core.config import repo_map
    monkeypatch.setattr(repo_map, "_load", lambda: {})
    from aiforge_core.memory import projects
    monkeypatch.setattr(projects, "_BOOT_REPO_ROOT", "")
    return root


def _facts(slug):
    from aiforge_core.memory import projects
    return projects._current_sections(slug).get("facts") or []


def test_lists_only_visible_child_folders(env):
    from aiforge_core.memory import projects
    assert projects.root() == str(env)
    assert [f["name"] for f in projects.list_folders()] == ["billing", "shop"]


def test_resolve_refuses_traversal_and_nested_names(env):
    from aiforge_core.memory import projects
    assert projects.resolve("shop") == str(env / "shop")
    for bad in ("..", "shop/..", "../repos", "", "missing", "/etc"):
        assert projects.resolve(bad) is None


def test_project_of_maps_a_cwd_to_its_top_folder(env):
    from aiforge_core.memory import projects
    (env / "shop" / "src").mkdir()
    assert projects.project_of(str(env / "shop" / "src")) == "shop"
    assert projects.project_of(str(env)) is None
    assert projects.project_of("/tmp") is None
    assert projects.project_of(None) is None


def test_capture_mirrors_the_brief_into_the_repo(env):
    from aiforge_core.memory import md_store, projects
    ent = projects.open_project(str(env / "shop"))
    md_store._brief_upsert("shop", "The checkout service listens on port 8443")
    projects.sync(ent["slug"])
    mirror = env / "shop" / ".aiforge" / "memory" / "MEMORY.md"
    assert "port 8443" in mirror.read_text()
    # nothing but .aiforge/ was created in the repo
    assert sorted(os.listdir(env / "shop")) == [".aiforge"]


def test_capture_hook_syncs_without_being_asked(env):
    from aiforge_core.memory import md_store, projects
    projects.open_project(str(env / "shop"))
    md_store.capture("project_learning",
                     "The checkout service retries a failed payment three times.",
                     repo="shop", classify=False, ingest=False)
    mirror = env / "shop" / ".aiforge" / "memory" / "MEMORY.md"
    assert "retries a failed payment" in mirror.read_text()


def test_an_edit_in_the_repo_is_imported(env):
    from aiforge_core.memory import md_store, projects
    ent = projects.open_project(str(env / "shop"))
    md_store._brief_upsert("shop", "Orders are stored in Postgres")
    md_store._brief_upsert("shop", "The old cart uses Redis")
    projects.sync(ent["slug"])
    mirror = env / "shop" / ".aiforge" / "memory" / "MEMORY.md"
    edited = mirror.read_text().replace("- The old cart uses Redis\n", "")
    edited = edited.replace("Orders are stored in Postgres",
                            "Orders are stored in Postgres 16")
    mirror.write_text(edited)
    res = projects.sync(ent["slug"])
    assert res["imported"] is True
    facts = _facts(ent["slug"])
    assert "Orders are stored in Postgres 16" in facts
    assert not any("Redis" in f for f in facts)


def test_both_sides_changed_keeps_the_new_fact_and_the_deletion(env):
    from aiforge_core.memory import md_store, projects
    ent = projects.open_project(str(env / "shop"))
    md_store._brief_upsert("shop", "Orders are stored in Postgres")
    md_store._brief_upsert("shop", "The old cart uses Redis")
    projects.sync(ent["slug"])
    mirror = env / "shop" / ".aiforge" / "memory" / "MEMORY.md"
    # the person deletes a fact in the repo…
    mirror.write_text(mirror.read_text().replace("- The old cart uses Redis\n", ""))
    # …while a chat learns a new one
    md_store._brief_upsert("shop", "Invoices are numbered per tenant")
    projects.sync(ent["slug"])
    facts = _facts(ent["slug"])
    assert "Invoices are numbered per tenant" in facts      # store's new fact kept
    assert "Orders are stored in Postgres" in facts
    assert not any("Redis" in f for f in facts)             # deletion not undone
    assert "Invoices are numbered per tenant" in mirror.read_text()


def test_existing_repo_file_and_existing_brief_are_unioned_on_first_open(env):
    from aiforge_core.memory import md_store, projects
    md_store._brief_upsert("shop", "Deploys go through the staging cluster")
    mdir = env / "shop" / ".aiforge" / "memory"
    mdir.mkdir(parents=True)
    (mdir / "MEMORY.md").write_text(
        "# shop\n\n## Facts\n\n- The API is versioned under /v2\n")
    ent = projects.open_project(str(env / "shop"))
    facts = _facts(ent["slug"])
    assert "The API is versioned under /v2" in facts
    assert "Deploys go through the staging cluster" in facts


def test_read_only_repo_degrades_instead_of_failing(env, monkeypatch):
    from aiforge_core.memory import md_store, projects
    ent = projects.open_project(str(env / "shop"))
    md_store._brief_upsert("shop", "Orders are stored in Postgres")

    def _deny(target, text, **_kw):
        if ".aiforge/memory/MEMORY.md" in str(target).replace(os.sep, "/"):
            raise PermissionError("read-only file system")
        return real(target, text, **_kw)

    real = projects._atomic.write_text
    monkeypatch.setattr(projects._atomic, "write_text", _deny)
    res = projects.sync(ent["slug"])
    assert res["ok"] is True and res["writable"] is False
    assert projects.read(ent["slug"])["writable"] is False
    assert "Postgres" in projects.read(ent["slug"])["text"]   # still in the store


def test_stale_is_only_facts_whose_files_are_all_gone(env):
    from aiforge_core.memory import projects
    shop = env / "shop"
    (shop / "src").mkdir()
    (shop / "src" / "cart.py").write_text("x")
    facts = [
        "Totals are computed in src/cart.py",                 # exists
        "Refunds are handled in src/refund.py",               # gone
        "cart.py and legacy_tax.py share the rounding rule",  # one exists → keep
        "Payments settle nightly",                            # names no file
        "See https://example.com/docs/setup.md for setup",    # a URL, not a file
        "The key lives in /etc/shop/secret.yaml",             # outside the repo
        "legacy_tax.py still rounds half up",                 # gone, bare name
    ]
    stale = {s["fact"] for s in projects.find_stale(facts, str(shop))}
    assert stale == {"Refunds are handled in src/refund.py",
                     "legacy_tax.py still rounds half up"}


def test_sweep_moves_stale_facts_out_and_restore_brings_them_back(env):
    from aiforge_core.memory import md_store, projects
    ent = projects.open_project(str(env / "shop"))
    slug = ent["slug"]
    md_store._brief_upsert("shop", "Refunds are handled in src/refund.py")
    md_store._brief_upsert("shop", "Payments settle nightly")
    assert projects.sweep_stale(slug)["moved"] == 1
    assert _facts(slug) == ["Payments settle nightly"]
    stale = projects.stale_list(slug)
    assert [s["fact"] for s in stale] == ["Refunds are handled in src/refund.py"]
    mirror = env / "shop" / ".aiforge" / "memory" / "MEMORY.md"
    assert "refund.py" not in mirror.read_text()
    assert projects.stale_restore(slug, stale[0]["fact"])["ok"] is True
    assert "Refunds are handled in src/refund.py" in _facts(slug)
    assert projects.stale_list(slug) == []


def test_compact_under_cap_does_not_call_the_model(env, monkeypatch):
    from aiforge_core.memory import md_store, projects
    from aiforge_core.memory.md_store import _compact_summarize
    ent = projects.open_project(str(env / "shop"))
    md_store._brief_upsert("shop", "Payments settle nightly")
    monkeypatch.setattr(_compact_summarize, "_summarize_notes",
                        lambda *_a, **_k: pytest.fail("model called under cap"))
    res = projects.compact(ent["slug"])
    assert res["ok"] is True and res["compacted"] is False


def test_compact_over_cap_folds_and_archives(env, monkeypatch):
    from aiforge_core.memory import md_store, projects
    from aiforge_core.memory.md_store import _compact_summarize
    monkeypatch.setenv("AIFORGE_PROJECT_MEMORY_CAP", "2000")
    ent = projects.open_project(str(env / "shop"))
    slug = ent["slug"]
    for i in range(60):
        md_store._brief_upsert(
            "shop", f"Rule number {i} says the settlement batch {i} runs after "
                    f"the ledger for region {i} has closed for the day")
    monkeypatch.setattr(_compact_summarize, "_summarize_notes",
                        lambda blocks, role: "## Summary\n\nSettlement runs "
                                             "after each regional ledger closes.")
    res = projects.compact(slug)
    assert res["compacted"] is True and res["chars_after"] < res["chars_before"]
    assert os.path.isfile(res["archived"])
    assert "Rule number 59" in open(res["archived"]).read()
    text = projects.read(slug)["text"]
    assert "Settlement runs after each regional ledger closes." in text
    assert _facts(slug) == []


def test_compact_leaves_the_brief_alone_when_no_model_answers(env, monkeypatch):
    from aiforge_core.memory import md_store, projects
    from aiforge_core.memory.md_store import _compact_summarize
    ent = projects.open_project(str(env / "shop"))
    md_store._brief_upsert("shop", "Payments settle nightly")
    monkeypatch.setattr(_compact_summarize, "_summarize_notes",
                        lambda *_a, **_k: None)
    res = projects.compact(ent["slug"], force=True)
    assert res["compacted"] is False and "error" in res
    assert _facts(ent["slug"]) == ["Payments settle nightly"]


def test_save_replaces_the_brief_and_reaches_the_repo(env):
    from aiforge_core.memory import md_store, projects
    ent = projects.open_project(str(env / "shop"))
    md_store._brief_upsert("shop", "Payments settle nightly")
    res = projects.save(ent["slug"],
                        "# shop\n\n## Facts\n\n- Payments settle hourly\n")
    assert res["ok"] is True
    assert _facts(ent["slug"]) == ["Payments settle hourly"]
    mirror = env / "shop" / ".aiforge" / "memory" / "MEMORY.md"
    assert "hourly" in mirror.read_text() and "nightly" not in mirror.read_text()


def test_promote_moves_a_fact_to_global(env):
    from aiforge_core.memory import md_store, projects
    ent = projects.open_project(str(env / "shop"))
    md_store._brief_upsert("shop", "Always squash commits before merging")
    md_store._brief_upsert("shop", "Payments settle nightly")
    res = projects.promote(ent["slug"], "- Always squash commits before merging")
    assert res == {"ok": True, "moved": 1}
    assert _facts(ent["slug"]) == ["Payments settle nightly"]
    assert "Always squash commits before merging" in _facts("shared")


def test_forget_archives_and_removes(env):
    from aiforge_core.memory import md_store, projects
    ent = projects.open_project(str(env / "shop"))
    slug = ent["slug"]
    md_store._brief_upsert("shop", "Payments settle nightly")
    projects.sync(slug)
    res = projects.forget(slug)
    assert res["ok"] is True and "Payments settle nightly" in open(res["archived"]).read()
    assert not md_store.brief_path(slug).exists()
    assert not (env / "shop" / ".aiforge" / "memory" / "MEMORY.md").exists()
    assert projects.entry(slug) is None


def test_instruction_files_are_read_once_per_content(env, monkeypatch):
    from aiforge_core.memory import instructions_ingest, projects
    (env / "shop" / "CLAUDE.md").write_text("# Build\n\nRun make build to compile.\n")
    ent = projects.register(str(env / "shop"))
    calls: list = []
    monkeypatch.setattr(
        instructions_ingest, "ingest_instruction_files",
        lambda files, **kw: calls.append((list(files), kw)) or {"ok": True, "captured": 1})
    assert projects.ingest_instructions(ent["slug"])["files"] == 1
    assert projects.ingest_instructions(ent["slug"])["files"] == 0   # unchanged
    (env / "shop" / "CLAUDE.md").write_text("# Build\n\nRun make all to compile.\n")
    assert projects.ingest_instructions(ent["slug"])["files"] == 1   # changed
    assert len(calls) == 2 and calls[0][1] == {"compact": False}


def test_scratch_chats_share_one_general_key(env, monkeypatch, tmp_path):
    from aiforge_core.memory import projects
    from aiforge_core.runtime.chat_agent import _chat_repo_key
    ws = tmp_path / "ws"
    (ws / "session-12").mkdir(parents=True)
    (ws / "session-13").mkdir()
    monkeypatch.setenv("AIFORGE_CHAT_WORKSPACE_ROOT", str(ws))
    assert _chat_repo_key(str(ws / "session-12")) == projects.GENERAL
    assert _chat_repo_key(str(ws / "session-13")) == projects.GENERAL
    assert _chat_repo_key(str(env / "shop")) == "shop"
