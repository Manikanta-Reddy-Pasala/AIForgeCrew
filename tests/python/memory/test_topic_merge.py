"""Topic-merge: consolidate near-duplicate topic briefs (gpsd/gpsd-config/…)."""
from __future__ import annotations
import importlib
import pytest


@pytest.fixture
def mem(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "m.db"))
    monkeypatch.setenv("AIFORGE_EMBED_BACKEND", "hash")
    monkeypatch.setenv("AIFORGE_OKR_SCOPE_LLM", "0")
    import aiforge_core.memory.sqlite_memory as sm
    importlib.reload(sm)
    return tmp_path


def test_clusters_prefix_family_and_fuzzy():
    from aiforge_core.memory import md_store as m
    keys = ["gpsd", "gpsd-config", "gpsd-configuration", "note", "notes",
            "gps-power-levels", "usbguard"]
    clusters = {tuple(c) for c in m._topic_clusters(keys)}
    # gpsd family collapses (canonical shortest first)
    assert ("gpsd", "gpsd-config", "gpsd-configuration") in clusters
    # note/notes fuzzy-merge
    assert ("note", "notes") in clusters
    # unrelated singletons are NOT clustered
    assert all("usbguard" not in c and "gps-power-levels" not in c
               for c in clusters)


def test_snap_topic_folds_prefix_family(mem):
    from aiforge_core.memory import md_store as m
    m._brief_upsert("gpsd", "gpsd fact one")
    # a new gpsd-config write snaps onto the existing 'gpsd' brief
    assert m._snap_topic("gpsd-config") == "gpsd"
    assert m._snap_topic("gpsd-configuration") == "gpsd"


def test_merge_similar_topics_consolidates(mem):
    from aiforge_core.memory import md_store as m
    from aiforge_core.runtime import work_notes
    for k, f in [("gpsd", "start gpsd with systemd"),
                 ("gpsd-config", "gpsd config lives in /etc/default/gpsd"),
                 ("gpsd-configuration", "set GPSD_OPTIONS in the config")]:
        m._brief_upsert(k, f, topic=k)
    assert len(m.iter_briefs()) == 3

    r = m.merge_similar_topics()
    assert r["merged"] == 2                       # 2 folded into gpsd
    briefs = {p.stem[len("compacted-"):] for p in m.iter_briefs()}
    assert "gpsd" in briefs
    assert "gpsd-config" not in briefs
    assert "gpsd-configuration" not in briefs
    # all three facts survive in the canonical brief
    body = work_notes.parse_note(m.brief_path("gpsd").read_text())["sections"]["facts"]
    joined = " ".join(body)
    assert "systemd" in joined
    assert "/etc/default/gpsd" in joined
    assert "GPSD_OPTIONS" in joined


def test_merge_protects_repo_briefs(mem, monkeypatch):
    from aiforge_core.memory import md_store as m
    # two repos that share a prefix must NOT be merged
    monkeypatch.setattr("aiforge_core.memory.migrations._discover_repos",
                        lambda: ["gps-tracker", "gps-tracker-web"])
    m._brief_upsert("gps-tracker", "repo one fact", topic=None)
    m._brief_upsert("gps-tracker-web", "repo two fact", topic=None)
    r = m.merge_similar_topics()
    assert r["merged"] == 0
    briefs = {p.stem[len("compacted-"):] for p in m.iter_briefs()}
    assert "gps-tracker" in briefs
    assert "gps-tracker-web" in briefs


# ─────────── aggressive family + typo merging (the /admin Topics sprawl) ─────

def test_family_collapses_single_word_prefix():
    from aiforge_core.memory import md_store as m
    keys = ["windows-ntp", "windows-cpu-mode", "windows-time-verify",
            "windows-w32tm-config", "unrelated-thing"]
    clusters = m._topic_clusters(keys)
    fam = next((c for c in clusters if "windows-ntp" in c), None)
    assert fam is not None
    assert len(fam) == 4
    assert all("unrelated-thing" not in c for c in clusters)


def test_two_word_prefix_and_typo_merge_even_with_families_off(monkeypatch):
    from aiforge_core.memory import md_store as m
    monkeypatch.setenv("AIFORGE_TOPIC_MERGE_FAMILIES", "0")
    keys = ["wifi-device-access", "wifi-device-connection",   # 2-word prefix
            "windows-ntp", "windows-npt",                     # transposition typo
            "vm-management", "vm-xml-patching"]               # 1-word only → stay
    clusters = {tuple(sorted(c)) for c in m._topic_clusters(keys)}
    assert ("wifi-device-access", "wifi-device-connection") in clusters
    assert ("windows-npt", "windows-ntp") in clusters
    # single-word family is NOT merged when families are off
    assert all("vm-management" not in c or "vm-xml-patching" not in c
               for c in clusters)


def test_merge_folds_family_into_the_common_prefix_topic(mem):
    from aiforge_core.memory import md_store as m
    for k, f in [("windows-ntp", "configure w32time as an NTP client"),
                 ("windows-npt-peers", "add peers to the NTP config"),
                 ("windows-cpu-mode", "set the CPU to high performance")]:
        m._brief_upsert(k, f, topic=k)
    assert len(m.iter_briefs()) == 3

    r = m.merge_similar_topics()
    assert r["merged"] == 3                       # all three fold into "windows"
    briefs = {p.stem[len("compacted-"):] for p in m.iter_briefs()}
    assert briefs == {"windows"}                  # one broad topic remains
    # the folded facts survived into the new canonical brief
    body = m.brief_path("windows").read_text(encoding="utf-8")
    assert "w32time" in body
    assert "peers" in body
    assert "CPU" in body


def test_family_merge_never_folds_a_protected_repo_or_shared(mem, monkeypatch):
    from aiforge_core.memory import md_store as m
    # 'shared' is protected; a 'shared-*' family must not swallow it.
    m._brief_upsert("shared", "a global fact")
    m._brief_upsert("shared-notes", "another", topic="shared-notes")
    r = m.merge_similar_topics()
    briefs = {p.stem[len("compacted-"):] for p in m.iter_briefs()}
    assert "shared" in briefs                      # protected, still present
