"""A condense saves the dropped messages; the note names the id; the id restores."""
import pytest

from aiforge_core.runtime import context_offload as co
from aiforge_core.runtime.chat_agent._context import _compaction as C
from aiforge_core.runtime.chat_agent._tools._memory import _t_memory_lookup


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))


def test_save_and_page_back_the_whole_text():
    text = "x" * 30000
    oid = co.save(text)
    assert oid and oid.startswith("off:")
    p1 = co.load(oid)
    assert len(p1["text"]) == 12000 and p1["next_offset"] == 12000
    p3 = co.load(oid, 24000)
    assert p3["text"] == "x" * 6000 and p3["next_offset"] is None
    assert co.save(text) == oid                      # same text, same id


def test_unknown_or_malformed_ids_load_nothing():
    assert co.load("off:deadbeef0000") is None
    assert co.load("../../etc/passwd") is None
    assert co.load("off:../x") is None


def test_the_note_points_at_the_saved_text():
    msgs = [{"role": "user", "content": "ask one"},
            {"role": "assistant", "content": "ACTION: file_read\nARGS_JSON: {\"path\": \"a.py\"}"}]
    oid = co.save(co.render(msgs))
    note = C._breadcrumb(msgs, "file_read×1", "", "", 1, oid)
    assert oid in note and "memory_lookup" in note
    assert "ask the user" in C._breadcrumb(msgs, "x", "", "", 1, None)


def test_memory_lookup_restores_by_id():
    oid = co.save("the dropped detail")
    out = _t_memory_lookup({"id": oid}, ".")
    assert out["ok"] and out["text"] == "the dropped detail"
    assert not _t_memory_lookup({"id": "off:nope"}, ".")["ok"]
    assert not _t_memory_lookup({}, ".")["ok"]


def test_follow_up_recall_is_on_by_default_and_has_an_off_switch(monkeypatch):
    from aiforge_core.runtime.chat_agent._turn import _blocks
    monkeypatch.delenv("AIFORGE_CHAT_FOLLOWUP_RECALL", raising=False)
    assert _blocks._followup_recall()
    monkeypatch.setenv("AIFORGE_CHAT_FOLLOWUP_RECALL", "0")
    assert not _blocks._followup_recall()
