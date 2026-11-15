"""Project memory stays with its project; global memory reaches every project.

Live case (real PosClientBackend chain): the project note was filed under the
"notes" bucket and the project rule under "rules" (both skipped by scoped
recall), so the project never recalled them; compaction then folded them into
the SHARED brief that an unrelated repo recalled. A global fact was filed
under whatever project the chat was in, so other projects never saw it.
"""
import sqlite3

from aiforge_core.runtime import rule_capture as rc


def _rows(cfg):
    con = sqlite3.connect(str(cfg / "memory.db"))
    return {s: r for r, s in con.execute("select repo, source from memory_units")}


def test_scope_routing(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    root = tmp_path / "PosClientBackend"
    root.mkdir()
    rc.store({"category": "rule", "scope": "project", "confidence": 0.9, "triggers": [],
              "canonical": "run maven offline"}, repo="PosClientBackend",
             session_id=1, repo_root=str(root))
    assert rc._store._scope_repo("project", "PosClientBackend", str(root), "rules") \
        not in ("rules", "notes", "shared")
    assert rc._store._scope_repo("global", "PosClientBackend", str(root), "notes") == "shared"
    assert rc._store._scope_repo("global", "PosClientBackend", str(root), "rules") == "rules"
    rows = _rows(tmp_path)
    assert rows.get("rules:PosClientBackend") not in ("rules", "notes", "shared", None)


def test_project_note_frontmatter_keeps_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    from aiforge_core.memory import md_store
    md_store.upsert_section(source="repo:X", title="X — project memory",
                            section_title="s", section_body="b", kind="project", repo="X")
    md_store.append_bullet(source="rules:X", title="X — rules", bullet="r", repo="X")
    texts = [p.read_text() for p in tmp_path.rglob("*.md")]
    assert sum("\nrepo: X\n" in t for t in texts) == 2
