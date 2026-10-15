"""The Compact button: compact the chat's context now, and keep the task going."""
from __future__ import annotations

import json

import pytest

from aiforge_core.runtime import chat_compact, chat_transcript


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_CHAT_COMPACT_WAIT_S", "0")
    return tmp_path


def _long_work(n=40, size=4000):
    out = []
    for i in range(n):
        out.append({"role": "user" if i % 2 == 0 else "assistant",
                    "content": (f"step {i} " + "x" * size)})
    out.append({"role": "assistant", "content": "done with the first part"})
    return out


def _save(sid, work):
    p = chat_transcript._path(sid)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"users": 3, "cpt": 4.0, "mark": "abc", "messages": work}))


def test_request_and_take():
    chat_compact.request(9)
    assert chat_compact.pending(9)
    assert chat_compact.take(9) and not chat_compact.take(9)
    assert not chat_compact.take(None)


def test_compact_saved_shrinks_and_keeps_the_link(cfg):
    work = _long_work()
    _save(3, work)
    got = chat_compact.compact_saved(3)
    assert got["ok"] and got["compacted"] and got["after"] < got["before"]
    data = json.loads(chat_transcript._path(3).read_text())
    assert data["users"] == 3 and data["mark"] == "abc"       # still continues the chat
    assert len(data["messages"]) < len(work)
    assert data["messages"][-1]["content"].endswith("done with the first part")


def test_compact_saved_without_a_transcript(cfg):
    got = chat_compact.compact_saved(4)
    assert got["ok"] and not got["compacted"] and "nothing to compact" in got["reason"]


def test_compact_running_condenses_the_live_conversation():
    class St:
        compact_key = None
        board = {}
        goal = "port the code"
        read_sigs_seen = {"a"}
        batch_mark = 0
        batch_unread = False
        pending_steps: list = []
    st = St()
    st.convo = [{"role": "system", "content": "sys"}] + _long_work()
    got = chat_compact.compact_running(st, "chat", None, 1)
    assert got and got["after"] < got["before"]
    assert st.convo[0]["role"] == "system" and not st.read_sigs_seen


def test_loop_hook_compacts_and_says_it_goes_on(monkeypatch):
    from aiforge_core.runtime.chat_agent import _loop
    monkeypatch.setattr(chat_compact, "take", lambda sid: True)
    monkeypatch.setattr(chat_compact, "compact_running", lambda st, r, c, s: {
        "before": 400000, "after": 40000, "messages_before": 80, "messages_after": 12})
    events = list(_loop._compact_on_request(object(), "chat", None, 1))
    assert "continuing the task" in events[0]["text"] and "80 → 12" in events[0]["text"]
    monkeypatch.setattr(chat_compact, "take", lambda sid: False)
    assert list(_loop._compact_on_request(object(), "chat", None, 1)) == []


def test_unfinished_last_turn(monkeypatch):
    from aiforge_core.runtime import chat_store
    rows = [{"role": "user", "content": "do it"},
            {"role": "assistant", "content": "x\n\n_Harness check: this answer does not look "
             "finished against your request_"}]
    monkeypatch.setattr(chat_store, "get_messages", lambda sid: rows)
    assert chat_compact.last_turn_unfinished(1)
    rows[-1] = {"role": "assistant", "content": "all done"}
    assert not chat_compact.last_turn_unfinished(1)
    rows[-1] = {"role": "assistant", "content": "partial", "steps": [{"type": "stopped"}]}
    assert chat_compact.last_turn_unfinished(1)



def test_a_short_history_of_big_reads_is_shrunk_too():
    """Live (10-15): the model read 12 files in a few big messages; folding
    whole messages had nothing to cut. Old tool output is shortened instead."""
    big = "OBSERVATION: " + ("def f(x):\n    return x\n" * 4000)
    class St:
        compact_key = None
        board = {}
        goal = "summarise"
        read_sigs_seen = set()
        batch_mark = 0
        batch_unread = False
        pending_steps: list = []
    st = St()
    st.convo = [{"role": "system", "content": "sys"},
                {"role": "user", "content": "read the files"},
                {"role": "assistant", "content": "ACTION: read_files"},
                {"role": "user", "content": big},
                {"role": "assistant", "content": "ACTION: read_files"},
                {"role": "user", "content": big},
                {"role": "assistant", "content": "ok"}]
    got = chat_compact.compact_running(st, "chat", None, 1)
    # the older read is shortened; the newest (read at this step) stays whole
    assert got and got["after"] < got["before"] * 0.6
    assert len(st.convo[3]["content"]) < 5000 and st.convo[5]["content"] == big



def test_a_request_is_not_carried_to_the_next_turn(monkeypatch):
    chat_compact.request(11)
    chat_compact.clear(11)
    assert not chat_compact.take(11)
    chat_compact.request(12)
    monkeypatch.setattr(chat_compact.time, "time", lambda: 10**10)
    assert not chat_compact.take(12)                  # too old


def test_a_newer_transcript_wins_over_the_compaction(cfg, monkeypatch):
    _save(5, _long_work())
    real = chat_transcript.replace_work

    def newer_then_replace(sid, work, expect=None):
        _save(5, _long_work(n=10))                     # a turn saved meanwhile
        return real(sid, work, expect=expect)
    monkeypatch.setattr(chat_transcript, "replace_work", newer_then_replace)
    got = chat_compact.compact_saved(5)
    assert not got["compacted"] and "moved on" in got["reason"]
    assert len(json.loads(chat_transcript._path(5).read_text())["messages"]) == 11


def test_idle_compaction_pins_the_latest_request(cfg):
    work = [{"role": "user", "content": "OLD REQUEST: build the parser"}] + _long_work() \
        + [{"role": "user", "content": "NEW REQUEST: add the tests"},
           {"role": "assistant", "content": "added the tests"}]
    _save(6, work)
    assert chat_compact.compact_saved(6)["compacted"]
    text = json.dumps(json.loads(chat_transcript._path(6).read_text())["messages"])
    assert "NEW REQUEST" in text
    assert "ORIGINAL TASK" not in text or "OLD REQUEST" not in text.split("ORIGINAL TASK")[1][:200]


def test_not_done_in_an_answer_is_not_unfinished(monkeypatch):
    from aiforge_core.runtime import chat_store
    rows = [{"role": "user", "content": "x"},
            {"role": "assistant", "content": "| item | NOT DONE |"}]
    monkeypatch.setattr(chat_store, "get_messages", lambda sid: rows)
    assert not chat_compact.last_turn_unfinished(1)
