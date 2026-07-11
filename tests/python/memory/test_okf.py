"""OKF v0.1 conformance helpers + injection into compaction/learner."""
import os
import tempfile

from aiforge_core.memory import okf


def test_frontmatter_has_type_and_recommended_fields_in_order():
    fm = okf.okf_frontmatter("learning", title="T", description="D",
                             tags=["a", "b"], timestamp="2026-07-11T00:00:00Z",
                             extra={"linked_krs": ["KR-1"]})
    assert fm.startswith("---\ntype: learning\n")
    # recommended fields in priority order
    order = [fm.index("title:"), fm.index("description:"),
             fm.index("tags:"), fm.index("timestamp:")]
    assert order == sorted(order)
    assert "linked_krs: [KR-1]" in fm       # custom key preserved


def test_validate_file():
    assert okf.validate_file("---\ntype: x\n---\nbody") == []
    assert okf.validate_file("no frontmatter")            # missing block
    assert okf.validate_file("---\ntitle: x\n---\n")      # no type
    assert okf.validate_file("# index only", is_reserved=True) == []


def test_append_log_newest_first_iso():
    p = os.path.join(tempfile.mkdtemp(), "log.md")
    okf.append_log(p, "old", date="2026-07-10")
    okf.append_log(p, "new", date="2026-07-11")
    okf.append_log(p, "new2", date="2026-07-11")
    txt = open(p).read()
    assert txt.index("## 2026-07-11") < txt.index("## 2026-07-10")   # newest first
    assert "- new2" in txt and "- new" in txt


def test_okf_rules_injected_into_producers():
    from aiforge_core.runtime import work_notes as wn
    from aiforge_core.runtime.prompts import learner
    assert "OPEN KNOWLEDGE FORMAT" in wn._CONSOLIDATE_SYS
    assert "Open Knowledge Format" in learner.PROMPT


def test_okr_store_writes_reserved_index_without_frontmatter(monkeypatch):
    monkeypatch.setenv("AIFORGE_OKR_ROOT", tempfile.mkdtemp())
    from aiforge_core.memory.okr import store
    store.save_node("objective", None, {"title": "O"}, "body")
    idx = os.path.join(store.okr_root(), "index.md")
    assert os.path.isfile(idx)
    assert not open(idx).read().startswith("---")       # reserved: NO frontmatter
    assert "/objectives/" in open(idx).read()           # absolute bundle link
