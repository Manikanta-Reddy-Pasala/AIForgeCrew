"""A stuck chat run changes approach and carries on; it does not pause to ask."""
import pytest

from aiforge_core.runtime.chat_agent._turn import _escalate as E


class _St:
    def __init__(self):
        self.convo = [{"role": "system", "content": "s"},
                      {"role": "user", "content": "OBSERVATION: x"}]
        self.role = "doer"


def _drive(gen):
    ev = []
    try:
        while True:
            ev.append(next(gen))
    except StopIteration as stop:
        return ev, stop.value


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("AIFORGE_CHAT_PAUSE_ON_STUCK", raising=False)
    monkeypatch.delenv("AIFORGE_CHAT_STUCK_ESCALATIONS", raising=False)
    monkeypatch.setattr(E, "_condense", lambda st: None)


def test_each_trip_sends_a_different_stronger_instruction():
    st = _St()
    texts = []
    for _ in range(3):
        ev, r = _drive(E.escalate(st, "You keep repeating `run_command`."))
        assert r == "continue"
        texts.append(st.convo[-1]["content"])
    assert len(set(texts)) == 3
    assert "simplest path to finishing" in texts[2]


def test_the_nudge_rides_on_the_last_user_turn_not_a_second_one():
    st = _St()
    _drive(E.escalate(st, "stuck."))
    assert [m["role"] for m in st.convo] == ["system", "user"]
    assert "OBSERVATION: x" in st.convo[-1]["content"]


def test_after_the_limit_it_asks_for_a_summary_then_ends_with_one(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_STUCK_ESCALATIONS", "2")
    st = _St()
    results = [_drive(E.escalate(st, "stuck."))[1] for _ in range(4)]
    assert results == ["continue", "continue", "continue", "wrap_up"]
    assert "Write `FINAL:` now" in st.convo[-1]["content"]


def test_zero_means_never_give_up(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_STUCK_ESCALATIONS", "0")
    st = _St()
    assert all(_drive(E.escalate(st, "x."))[1] == "continue" for _ in range(40))


def test_the_old_pause_is_one_setting_away(monkeypatch):
    assert not E.pause_on_stuck()
    monkeypatch.setenv("AIFORGE_CHAT_PAUSE_ON_STUCK", "1")
    assert E.pause_on_stuck()


def test_a_run_that_repeats_one_action_is_steered_and_ends_with_a_summary(
        tmp_path, monkeypatch):
    """End to end: the model repeats the same call forever; the loop condenses
    and changes the instruction instead of pausing, and once told to wrap up the
    model's FINAL ends the turn. No awaiting_input pause anywhere."""
    from aiforge_core.runtime import chat_agent as ca
    monkeypatch.setenv("AIFORGE_CHAT_STUCK_ESCALATIONS", "2")
    (tmp_path / "a.txt").write_text("hello")

    def fn(role, convo):
        last = "\n".join(str(m.get("content")) for m in convo[-3:])
        if "Write `FINAL:` now" in last:
            return "FINAL: read a.txt; nothing else was possible"
        return 'ACTION: run_command\nARGS_JSON: {"cmd": "false"}'

    evs = list(ca.run_chat_agent(
        [{"role": "user", "content": "do the thing"}],
        cwd=str(tmp_path), complete_fn=fn, max_steps=None))
    assert not any(e.get("awaiting_input") for e in evs)
    msgs = [e for e in evs if e.get("type") == "message"]
    assert msgs and "nothing else was possible" in msgs[-1]["text"]


def test_the_same_call_with_the_same_result_is_a_loop_even_when_the_workspace_keeps_moving(
        tmp_path, monkeypatch):
    """Live: `sudo rm -f <path>` ran dozens of times. Each call succeeded and the
    workspace fingerprint kept changing for unrelated reasons, so the per-state
    counters and the recovery budget never ran out. Identical call + identical
    result, in a row, is enough."""
    from aiforge_core.runtime import chat_agent as ca
    from aiforge_core.runtime.chat_agent._turn import _action as A
    monkeypatch.setenv("AIFORGE_CHAT_STUCK_ESCALATIONS", "2")
    monkeypatch.setattr(A, "strike", lambda *a, **k: "")          # per-state guard defeated
    monkeypatch.setattr(A, "may_recover", lambda st: True)        # budget never runs out
    monkeypatch.setattr(A, "note_step", lambda *a, **k: None)     # idle-step guard fooled too
    runs = {"n": 0}

    def fn(role, convo):
        last = "\n".join(str(m.get("content")) for m in convo[-3:])
        if "Write `FINAL:` now" in last:
            return "FINAL: the path was already gone; nothing else to do"
        runs["n"] += 1
        # The wording varies every time (as a real model's does), so the
        # identical-REPLY guard cannot be what stops it.
        return (f"THOUGHT: attempt {runs['n']}, it may still be there\n"
                'ACTION: run_command\nARGS_JSON: {"cmd": "echo gone"}')

    evs = list(ca.run_chat_agent(
        [{"role": "user", "content": "clear the preprocessed configs"}],
        cwd=str(tmp_path), complete_fn=fn, max_steps=None))
    msgs = [e for e in evs if e.get("type") == "message"]
    assert msgs and "already gone" in msgs[-1]["text"]
    assert runs["n"] < 40                       # it stopped repeating, it did not run forever


def test_identical_run_counting():
    from aiforge_core.runtime.chat_agent._turn import _progress as P

    class S:
        pass
    st = S()
    for _ in range(3):
        P.note_identical(st, "run_command|a", {"ok": True, "stdout": ""})
    assert P.identical_repeats(st, "run_command|a") == 3
    P.note_identical(st, "run_command|a", {"ok": True, "stdout": "changed"})
    assert P.identical_repeats(st, "run_command|a") == 1       # a new result resets it
    P.note_identical(st, "run_command|b", {"ok": True, "stdout": "changed"})
    assert P.identical_repeats(st, "run_command|a") == 0       # another call resets it
