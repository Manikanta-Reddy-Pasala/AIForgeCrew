"""A goal changed mid-turn: the system prompt's GOALS block and the turn's
request follow, so the model never reads the old goal as the one to do."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from aiforge_core.runtime import chat_goals as G
from aiforge_core.runtime.chat_agent._turn import _limits as L


@pytest.fixture(autouse=True)
def _cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_CHAT_GOALS", "1")
    monkeypatch.setattr(G, "_temporary", lambda sid: False)


def _st(sid=9):
    G._save(sid, {**G._empty(), "seq": 2, "goals": [
        {"id": "g1", "text": "add coupons", "status": "open", "turn": 1},
        {"id": "g2", "text": "write docs", "status": "open", "turn": 1}],
        "now": ["g1", "g2"], "current": "add coupons / write docs"})
    old = G.block(sid)
    return SimpleNamespace(session_id=sid, builder=None, strict_finish=False,
                           goals_block=old, goal="add coupons and write docs",
                           convo=[{"role": "system", "content": old + "\n\nYou are an agent."},
                                  {"role": "user", "content": "add coupons and write docs"}])


def test_a_dropped_goal_leaves_the_system_prompt_and_the_request():
    st = _st()
    out = G.user_edit(9, "g2", status="dropped")
    L._sync_goals(st, 9, [f"[GOALS] Goal {out['change']}"])
    sys = st.convo[0]["content"]
    assert sys.count(G.MARK) == 1 and sys.endswith("You are an agent.")
    assert "GOAL NOW: add coupons" in sys and "(working on now): write docs" not in sys
    assert st.goals_cut and st.goal == "add coupons and write docs"


def test_an_added_goal_swaps_the_block_and_keeps_the_request():
    st = _st()
    G.user_add(9, "add refunds", in_hand=True)
    L._sync_goals(st, 9, ["[GOALS] Goal added g3: add refunds"])
    assert "add refunds" in st.convo[0]["content"]
    assert st.goal == "add coupons and write docs"


def test_a_steer_goal_pass_change_counts_too():
    st = _st()
    G.user_edit(9, "g1", text="add coupons with expiry")
    st.goal_changes = ["changed g1: add coupons with expiry"]
    L._sync_goals(st, 9)
    assert "add coupons with expiry" in st.convo[0]["content"]
    assert st.goal.startswith("add coupons and write docs") and "expiry" in st.goal
    assert not getattr(st, "goals_cut", False)


def test_the_block_is_swapped_in_a_compressed_turn_note_too():
    st = _st()
    old = st.convo[0]["content"].split("\n\nYou are")[0]
    st.convo[0]["content"] = "You are an agent."                  # carry-on mode
    st.convo[1]["content"] = "do it\n\n" + old.replace("\n- ", "\n-  ") + "\n\nmore"
    G.user_edit(9, "g2", status="dropped")
    blk = L._sync_goals(st, 9, ["[GOALS] Goal dropped g2: write docs"])
    assert blk == "" and st.convo[1]["content"].count(G.MARK) == 1
    assert "(working on now): write docs" not in st.convo[1]["content"]
    assert st.convo[1]["content"].endswith("\n\nmore") and st.convo[0]["content"] == "You are an agent."


def test_no_block_anywhere_is_handed_back_for_the_note():
    st = _st()
    st.convo[0]["content"] = "You are an agent."
    G.user_add(9, "add refunds", in_hand=True)
    assert G.MARK in L._sync_goals(st, 9, ["[GOALS] Goal added g3: add refunds"])
    assert st.convo[0]["content"] == "You are an agent."


def test_a_word_in_a_goal_text_is_no_edit():
    st = _st()
    G.user_add(9, "show what changed today", in_hand=True)
    L._sync_goals(st, 9, ["[GOALS] Goal added g3: show what changed today"])
    assert not getattr(st, "goals_cut", False) and st.goal == "add coupons and write docs"


def test_a_goal_added_but_never_shown_is_not_closed():
    _st()
    data = G._load(9)
    data["shown"] = ["g1", "g2"]
    G._save(9, data)
    G.user_add(9, "add refunds", in_hand=True)              # in now, never told
    G._PASSED.add("9")
    G.finish_turn(9, completed=True)
    st = {g["id"]: g["status"] for g in G._load(9)["goals"]}
    assert st == {"g1": "done", "g2": "done", "g3": "open"}


def test_drop_open_drops_nothing_when_it_cannot_tell():
    G._save(9, {**G._empty(), "seq": 2, "goals": [
        {"id": "g1", "text": "add coupons", "status": "open", "turn": 1},
        {"id": "g2", "text": "write docs", "status": "open", "turn": 1}], "now": []})
    assert G.drop_open(9, like="zzz qqq") == []
    assert G.drop_open(9, like="the docs to write") == ["write docs"]


def test_goals_are_off_inside_a_job_run():
    assert G.enabled()
    with G.off_for_run():
        assert not G.enabled() and G.block(9) == ""
    assert G.enabled()


def test_the_job_runner_switches_goals_off_for_every_step():
    from aiforge_core.jobs import scheduler

    def gen():
        for _ in range(3):
            yield G.enabled()
    assert list(scheduler._goals_off(gen(), G)) == [False, False, False]
