"""A growing note (rule book, session note) keeps ONE memory row per source.

Live case: the OrderService rule book had two rows after two rules — the
first held only rule 1 — so recall could return the stale copy.
"""
import sqlite3

from aiforge_core.memory import md_store


def _rows(cfg, source):
    con = sqlite3.connect(str(cfg / "memory.db"))
    return [t for (t,) in con.execute("select text from memory_units where source=?", (source,))]


def test_bullets_and_sections_replace_their_row(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    md_store.append_bullet(source="rules:X", title="X — rules", bullet="rule one", repo="X")
    md_store.append_bullet(source="rules:X", title="X — rules", bullet="rule two", repo="X")
    rows = _rows(tmp_path, "rules:X")
    assert len(rows) == 1 and "rule one" in rows[0] and "rule two" in rows[0]
    for i in range(3):
        md_store.upsert_section(source="chat-session:1", title="s", section_title=f"t{i}",
                                section_body=f"turn {i}")
    rows = _rows(tmp_path, "chat-session:1")
    assert len(rows) == 1 and "turn 2" in rows[0]
