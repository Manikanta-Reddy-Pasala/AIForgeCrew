"""A reply that is only the system's `[did: …]` action log is not an answer; a
message with a target and 'keep going' gets the goal-loop rule."""
import pytest

from aiforge_core.runtime.chat_agent._guards import echo as E
from aiforge_core.runtime.chat_agent._turn._convo import wants_goal_loop


def test_the_log_is_stripped_and_a_lead_in_with_a_colon_is_an_echo():
    text = ("Honest re-check — verifying what is actually on disk versus what "
            "I claimed:\n[did: file_read(a)✓, file_read(a)✓, run_command(ls)✓]")
    clean, echo = E.strip_action_log(text)
    assert "[did:" not in clean and echo
    clean, echo = E.strip_action_log("[did: grep✓]")
    assert clean == "" and echo


def test_a_real_answer_with_a_trailing_log_keeps_the_answer():
    clean, echo = E.strip_action_log(
        "The read path takes 480 ms for 50k rows; the index fixed it.\n[did: grep✓]")
    assert clean.startswith("The read path takes 480 ms") and not echo
    assert E.strip_action_log("plain answer") == ("plain answer", False)


def test_a_run_whose_reply_is_only_the_log_is_sent_back_to_work(tmp_path):
    from aiforge_core.runtime import chat_agent as ca
    calls = {"n": 0}

    def fn(role, convo):
        calls["n"] += 1
        last = str(convo[-1].get("content"))
        if "only a log of earlier actions" in last:
            return "FINAL: the query takes 480 ms for 50k rows after the index."
        return "FINAL: Honest re-check:\n[did: file_read(p)✓, file_read(p)✓]"

    evs = list(ca.run_chat_agent([{"role": "user", "content": "check the read speed"}],
                                 cwd=str(tmp_path), complete_fn=fn))
    msgs = [e["text"] for e in evs if e.get("type") == "message"]
    assert msgs and "480 ms" in msgs[-1] and "[did:" not in msgs[-1]


def test_a_model_that_only_ever_writes_the_log_ends_with_a_plain_message(tmp_path):
    from aiforge_core.runtime import chat_agent as ca
    evs = list(ca.run_chat_agent(
        [{"role": "user", "content": "check the read speed"}], cwd=str(tmp_path),
        complete_fn=lambda r, c: "FINAL: Re-check:\n[did: file_read(p)✓]"))
    msgs = [e["text"] for e in evs if e.get("type") == "message"]
    assert msgs and "[did:" not in msgs[-1] and "could not produce a result" in msgs[-1]


@pytest.mark.parametrize("text,yes", [
    ("it must be like seamless read, our target is 500 milliseconds for 50k records", True),
    ("loop until the benchmark passes", True),
    ("keep going until all tests are green", True),
    ("the query must be under 200 ms", True),
    ("don't stop until it works", True),
    ("explain how a for loop works", False),
    ("rename the helper and update the imports", False),
])
def test_goal_loop_cue(text, yes):
    assert wants_goal_loop(text) is yes


def test_goal_loop_rule_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_GOAL_LOOP", "0")
    assert not wants_goal_loop("keep going until it passes")
