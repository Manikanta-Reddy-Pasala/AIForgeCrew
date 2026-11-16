"""Memory notes never keep terminal colour codes from tool output.

Live case: a real OrderService session note stored
"[1;31mERROR[m] [1m[Help 1][m ..." and recall showed that garbage.
"""
from aiforge_core.memory import md_store

RED = "\x1b[1;31mERROR\x1b[m] \x1b[1m[Help 1]\x1b[m see log"


def test_upsert_section_and_bullet_strip_codes(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    md_store.upsert_section(source="chat-session:1", title="t", section_title="s",
                            section_body=RED)
    md_store.append_bullet(source="rules:r", title="r rules", bullet=RED)
    text = "".join(p.read_text() for p in tmp_path.rglob("*.md"))
    assert "\x1b" not in text and "ERROR] [Help 1] see log" in text
