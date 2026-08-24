"""map_scopes writes TYPED, directed relationship links (a depends-on b ⇒ b
required-by a) into the OKR Links section, and expand_links reads the type."""
from __future__ import annotations
import importlib
import types
import pytest


@pytest.fixture
def mem(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "m.db"))
    monkeypatch.setenv("AIFORGE_EMBED_BACKEND", "hash")
    monkeypatch.setenv("AIFORGE_OKR_SCOPE_LLM", "1")
    import aiforge_core.memory.sqlite_memory as sm
    importlib.reload(sm)
    return tmp_path


def test_map_scopes_writes_typed_and_inverse_links(mem, monkeypatch):
    from aiforge_core.memory import md_store as m
    m._brief_upsert("time-sync", "chrony consumes gpsd via SHM refclock 0")
    m._brief_upsert("gpsd", "gpsd reads the GPS receiver on /dev/ttyUSB0")

    # stub the LLM edge proposal: time-sync depends-on gpsd
    def _fake(role, msgs, model, **kw):
        return model(edges=[{"a": "time-sync", "b": "gpsd", "type": "depends-on"}])
    monkeypatch.setattr("aiforge_core.llm.structured.structured_complete", _fake)

    r = m.map_scopes()
    assert r["edges"] == 1
    ts = m._parse_brief(m.brief_path("time-sync").read_text())["links"]
    gp = m._parse_brief(m.brief_path("gpsd").read_text())["links"]
    assert ts == ["depends-on: [gpsd](compacted-gpsd.md)"]
    assert gp == ["required-by: [time-sync](compacted-time-sync.md)"]  # inverse


def test_expand_links_surfaces_relationship_type(mem, monkeypatch):
    from aiforge_core.memory import md_store as m
    m._brief_upsert("gpsd", "gpsd reads the GPS receiver on /dev/ttyUSB0")
    m._brief_upsert("time-sync", "chrony consumes gpsd")
    def _fake(role, msgs, model, **kw):
        return model(edges=[{"a": "time-sync", "b": "gpsd", "type": "depends-on"}])
    monkeypatch.setattr("aiforge_core.llm.structured.structured_complete", _fake)
    m.map_scopes()

    # a hit on time-sync expands to the linked gpsd brief, tagged with the rel
    linked = m.expand_links(["compacted:compacted-time-sync"])
    gpsd = next((d for d in linked if d["key"] == "gpsd"), None)
    assert gpsd is not None
    assert gpsd["rel"] == "depends-on"
    assert "depends-on" in gpsd["text"]           # relationship surfaced in recall text


def test_plain_link_still_parses_and_relates_to_omits_prefix(mem, monkeypatch):
    from aiforge_core.memory import md_store as m
    m._brief_upsert("a", "fact a"); m._brief_upsert("b", "fact b")
    def _fake(role, msgs, model, **kw):
        return model(edges=[{"a": "a", "b": "b", "type": "relates-to"}])
    monkeypatch.setattr("aiforge_core.llm.structured.structured_complete", _fake)
    m.map_scopes()
    # relates-to is the default → rendered as a PLAIN link (no prefix), OKR-clean
    assert m._parse_brief(m.brief_path("a").read_text())["links"] == \
        ["[b](compacted-b.md)"]
