"""Compaction never folds or archives a rule book.

Live case: a real PosClientBackend chain compacted the project rule book into
the project brief and archived the file, so the mandatory rules block of the
next session lost "run Maven offline" and "needs JDK 24".
"""
from aiforge_core.memory import md_store
from aiforge_core.memory.md_store import _compact_plan


def test_rule_books_are_not_compaction_input(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    md_store.append_bullet(source="rules:Pcb", title="Pcb — rules", bullet="run maven offline",
                           repo="Pcb")
    md_store.append_bullet(source="rules:global", title="AIForge rules", bullet="be short")
    md_store.upsert_section(source="repo:Pcb", title="Pcb — project memory", section_title="s",
                            section_body="fixed labels", kind="project", repo="Pcb")
    for axis in ("repo", "topic", "kind"):
        srcs = {d.get("source") for d in _compact_plan._live_capture_notes(axis)}
        assert not any(s.startswith("rules:") for s in srcs), axis
    assert "repo:Pcb" in {d.get("source") for d in _compact_plan._live_capture_notes("repo")}
