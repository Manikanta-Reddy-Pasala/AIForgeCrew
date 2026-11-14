"""Topic names: same subject by stem / abbreviation / separators / suffix
family; split topics re-cut into one clean set of parts."""
from __future__ import annotations

import pytest

from aiforge_core.memory.md_store._graph import _reconcile_names as N


@pytest.mark.parametrize("a,b", [
    ("venv-setup", "virtual-environment-setup"),
    ("aiforgememory", "ai-forge-memory"),
    ("ai-forge-memory-2", "aiforgememory"),
    ("persistence", "persistent"),
    ("test", "testing"),
    ("validation", "input-validation"),
    ("test", "end-to-end-testing"),
    ("retry", "error-retry"),
])
def test_same_subject_names(a, b):
    assert N._same_name(a, b)


@pytest.mark.parametrize("a,b", [
    ("cli", "client"),                 # stem guard
    ("memory", "in-memory"),           # a modifier changes the meaning
    ("java", "javascript"),
    ("bank", "feature-names"),         # names alone say nothing (embeddings decide)
    ("c", "cpp"),
])
def test_different_subject_names(a, b):
    assert not N._same_name(a, b)


def test_canonical_prefers_a_word_name(monkeypatch):
    from aiforge_core.memory.md_store._graph import _reconcile_merge as M
    monkeypatch.setattr(M, "_common_token_prefix", lambda c: "")
    assert M._canonical_name(sorted(["mx2-5044", "pipeline", "qwen-big-pipeline"], key=len), set()) == "pipeline"
    assert M._canonical_name(["c", "cpp-tools"], set()) == "cpp-tools"
    assert M._canonical_name(["retry", "posclientbackend"], {"posclientbackend"}) == "posclientbackend"


def test_repage_recuts_a_rotten_split_topic(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "memory"))
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    from aiforge_core.memory import md_store
    from aiforge_core.memory.md_store._base import briefs_dir
    d = briefs_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / "compacted-topic.md").write_text(
        "# Topic memory (compacted) (part 1/2)\n\n## Facts\n- fact one\n"
        "- Part 2 of 3 · main topic: [topic](compacted-topic.md)\n- fact two\n")
    (d / "compacted-topic-2.md").write_text(
        "# Topic memory (compacted) (part 2/2)\n\n## Facts\n- fact three\n- fact one\n")
    (d / "compacted-topic-2-2.md").write_text(
        "# Topic 2 2 memory (compacted)\n\n**Part 2 of 2** · main topic: "
        "[topic-2](compacted-topic-2.md)\n\n## Facts\n- fact four\n")
    out = md_store.repage_split_topics()
    assert out["repaged"] == 1
    names = sorted(p.name for p in d.glob("compacted-*.md"))
    assert names == ["compacted-topic.md"]                 # everything fits one file again
    text = (d / "compacted-topic.md").read_text()
    for f in ("fact one", "fact two", "fact three", "fact four"):
        assert text.count(f) == 1
    assert "Part 2 of 3" not in text
