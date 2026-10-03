"""The WORKING model decides what a message means — not a router, not a rule.

Live messages the word rules got wrong, one after another:

* "its ok continue simplfying all files .. use kiss and seperation of ceoncers.."
  was neither a go-ahead nor a change request for them: the agent answered
  "Nothing was written this turn" and that was accepted as the result;
* "yes continue" after a plan started a turn that did nothing;
* "last commit is 47 minutes ago, when did you commit" is a question.

A separate classifier call would see three truncated messages and no context.
The model doing the work has the whole conversation, so the harness asks IT,
once, in its own context, when a turn is about to end with no file changed.
These tests drive the real loop with a scripted model.

(The fourth live message, "we can't run python application right ? … use
ours", arrived while a run was going. Such a message is not classified either:
it is handed to the running model — tests/python/api/test_chat_side_tasks.py.)
"""
import subprocess

import pytest

from aiforge_core.runtime import chat_agent as ca
from aiforge_core.runtime.chat_agent._guards import zero_edit

SIMPLIFY = "its ok continue simplfying all files .. use kiss and seperation of ceoncers.."
QUESTION = "last commit is 47 minutes ago, when did you commit"
CHECK_HEAD = "[harness — not the user] A routine check before this turn ends."
WRITE = ('ACTION: file_write\nARGS_JSON: {"path": "config.rs", '
         '"content": "pub struct Config;\\n"}')
READ = 'ACTION: file_read\nARGS_JSON: {"path": "main.rs"}'


@pytest.fixture
def cwd(tmp_path):
    run = lambda *a: subprocess.run(["git", *a], cwd=tmp_path, capture_output=True, check=True)
    run("init", "-q"); run("config", "user.email", "t@t"); run("config", "user.name", "t")
    (tmp_path / "main.rs").write_text("fn main() {}\n")
    run("add", "-A"); run("commit", "-q", "-m", "init")
    return tmp_path


def _drive(history, cwd, script):
    """Run the loop with ``script(last_message, n) -> reply``. Returns the
    final message, every message the model was sent last, and the events."""
    seen = []

    def fn(role, convo):
        seen.append(str(convo[-1].get("content")))
        return script(seen[-1], len(seen))

    evs = list(ca.run_chat_agent(history, cwd=str(cwd), complete_fn=fn))
    msgs = [e["text"] for e in evs if e.get("type") == "message"]
    return (msgs[-1] if msgs else ""), seen, evs


PLAN_CHAT = [
    {"role": "user", "content": "rewrite the poller into config.rs, read.rs and publish.rs"},
    {"role": "assistant", "content": "Plan: 1. move Config to config.rs. 2. add read.rs. "
                                     "Say start and I will do step 1."},
    {"role": "user", "content": "yes continue"},
]
SIMPLIFY_CHAT = [
    {"role": "user", "content": "rewrite the python modules using kiss and separation of concern"},
    {"role": "assistant", "content": "Split cluster_handling.py. 14 files remain."},
    {"role": "user", "content": SIMPLIFY},
]


@pytest.mark.parametrize("history", [PLAN_CHAT, SIMPLIFY_CHAT])
def test_a_stalling_model_gets_the_in_context_check_and_then_acts(cwd, history):
    """The model ends the turn with a plan and no edit. It is asked once, in
    its own context; it reads the user's message with the conversation, sees
    that work was asked for, and does it."""
    def script(last, n):
        if last.startswith(CHECK_HEAD):
            return WRITE
        if "OBSERVATION" in last and "config.rs" in last:
            return "FINAL: Wrote config.rs (step 1)."
        return "FINAL: Here is what remains: config.rs, read.rs, publish.rs."

    final, seen, evs = _drive(history, cwd, script)
    assert (cwd / "config.rs").exists()
    assert final == "Wrote config.rs (step 1)."
    checks = [s for s in seen if s.startswith(CHECK_HEAD)]
    assert len(checks) == 1 and checks[0] == zero_edit.CHECK
    assert len(seen) == 3                         # plan, the work, the result
    # the firm reminder was not needed: the model acted on the check
    assert not any("ALREADY told you to go ahead" in s for s in seen)
    # the user sees why the turn went on
    assert any(e.get("role") == "system" and "no file was changed" in e.get("text", "")
               for e in evs if e.get("type") == "thought")


def test_the_check_is_the_same_note_whatever_the_wording(cwd):
    """No rule reads the message: "same for the rest pls" has no go-ahead
    word and no change verb, and is checked like any other."""
    history = SIMPLIFY_CHAT[:2] + [{"role": "user", "content": "same for the rest pls"}]
    final, seen, _ = _drive(history, cwd, lambda last, n: WRITE if last.startswith(CHECK_HEAD)
                            else "FINAL: Simplified the rest." if "OBSERVATION" in last
                            else "FINAL: The rest could be simplified the same way.")
    assert (cwd / "config.rs").exists() and final == "Simplified the rest."


def test_a_model_that_admits_it_did_nothing_after_the_check_is_pushed_then_labelled(cwd):
    stall = "Nothing was written this turn. Say start and I will do step 1 in the next turn."
    final, seen, _ = _drive(PLAN_CHAT, cwd, lambda last, n: "FINAL: " + stall)
    assert sum(s.startswith(CHECK_HEAD) for s in seen) == 1           # asked once
    assert sum("ALREADY told you to go ahead" in s for s in seen) == 3  # then pushed
    assert final == zero_edit.DISCLAIMER + stall                       # then labelled
    assert not (cwd / "config.rs").exists()


def test_same_does_not_turn_a_stall_into_a_result(cwd):
    """"Nothing was written, say start" after "yes continue", and then SAME to
    the check: the stall is still a stall."""
    stall = "Nothing was written this turn. Say start and I will do step 1 in the next turn."

    def script(last, n):
        if last.startswith(CHECK_HEAD):
            return "SAME"
        if "ALREADY told you to go ahead" in last:
            return WRITE
        if "OBSERVATION" in last and "config.rs" in last:
            return "FINAL: Wrote config.rs (step 1)."
        return "FINAL: " + stall

    final, seen, _ = _drive(PLAN_CHAT, cwd, script)
    assert (cwd / "config.rs").exists() and final == "Wrote config.rs (step 1)."
    assert sum("ALREADY told you to go ahead" in s for s in seen) == 1


def test_a_pure_question_costs_no_extra_step(cwd):
    final, seen, evs = _drive(
        [{"role": "user", "content": "what does main.rs do?"}], cwd,
        lambda last, n: "FINAL: It is an empty main().")
    assert final == "It is an empty main()." and len(seen) == 1
    assert not any("no file was changed" in e.get("text", "") for e in evs)


def test_a_question_the_model_looked_something_up_for_costs_no_extra_step(cwd):
    final, seen, _ = _drive(
        [{"role": "user", "content": "what does main.rs do?"}], cwd,
        lambda last, n: READ if n == 1 else "FINAL: It is an empty main().")
    assert final == "It is an empty main()." and len(seen) == 2
    assert not any(s.startswith(CHECK_HEAD) for s in seen)


QUESTION_CHAT = [{"role": "user", "content": "simplify all files and commit"},
                 {"role": "assistant", "content": "Done and committed."},
                 {"role": "user", "content": QUESTION}]
ANSWER = "The last commit was made at 14:02, 47 minutes ago."


@pytest.mark.parametrize("word", [
    "SAME", "FINAL: SAME", "same.", "**SAME**", "SAME — it was a question.",
    "The user only asked when I committed — that is a question, and I already "
    "answered it. No file work was requested or left undone. SAME",
])
def test_a_question_after_work_keeps_the_answer_the_model_already_gave(cwd, word):
    """The model looked something up (a tool ran), so the turn is checked once.
    It reads the message as a question and says so with one word: the answer it
    had already written goes to the user as it is — one short extra step, no
    label, no push to edit, and no reply-to-the-harness in place of the answer
    (live, a model wrote "You asked a question, not for a change. Nothing to
    do." where the answer should have been)."""
    def script(last, n):
        if n == 1:
            return READ
        return word if last.startswith(CHECK_HEAD) else "FINAL: " + ANSWER

    final, seen, evs = _drive(QUESTION_CHAT, cwd, script)
    assert final == ANSWER
    assert sum(s.startswith(CHECK_HEAD) for s in seen) == 1 and len(seen) == 3
    assert not any("ALREADY told you" in s for s in seen)
    # the answer reaches the user once: it is not also shown as a thought
    assert not any(e.get("type") == "thought" and e.get("text") == ANSWER for e in evs)


def test_a_new_answer_after_the_check_replaces_the_old_one(cwd):
    def script(last, n):
        if n == 1:
            return READ
        return ("FINAL: Correction: it was 52 minutes ago, at 13:57."
                if last.startswith(CHECK_HEAD) else "FINAL: " + ANSWER)

    final, _seen, _ = _drive(QUESTION_CHAT, cwd, script)
    assert final == "Correction: it was 52 minutes ago, at 13:57."
    assert "(No file was changed" not in final


def test_an_answer_that_merely_starts_with_the_word_is_an_answer(cwd):
    new = "Same as before: the poller reads from ClickHouse and then publishes."

    def script(last, n):
        if n == 1:
            return READ
        return "FINAL: " + new if last.startswith(CHECK_HEAD) else "FINAL: " + ANSWER

    final, _seen, _ = _drive(QUESTION_CHAT, cwd, script)
    assert final == new


def test_a_question_back_to_the_user_after_the_check_stands(cwd):
    def script(last, n):
        if n == 1:
            return READ
        return ("FINAL: Which commit do you mean, the one on main or on the branch?"
                if last.startswith(CHECK_HEAD) else "FINAL: " + ANSWER)

    final, _seen, _ = _drive(QUESTION_CHAT, cwd, script)
    assert final.startswith("Which commit do you mean")


def test_a_change_that_cannot_be_made_is_said_and_labelled(cwd):
    final, seen, _ = _drive(
        [{"role": "user", "content": "port the poller to rust"}], cwd,
        lambda last, n: "FINAL: NOT DONE: there is no rust toolchain on this machine."
        if last.startswith(CHECK_HEAD) else "FINAL: I would need cargo for this.")
    assert final == zero_edit.DISCLAIMER + "there is no rust toolchain on this machine."
    assert len(seen) == 2


def test_a_final_that_asks_the_user_a_real_question_is_not_second_guessed(cwd):
    final, seen, _ = _drive(
        [{"role": "user", "content": "add a cache to the read path"}], cwd,
        lambda last, n: "FINAL: Which store should the cache use, redis or in-process?")
    assert final.startswith("Which store") and len(seen) == 1


def test_the_check_fires_on_a_clean_committed_tree(cwd):
    """A chat's workspace is committed at the start of every turn: the tree is
    clean before and after a turn that did nothing. That is an unchanged tree
    (the same commit), not "no signal"."""
    import subprocess
    assert subprocess.run(["git", "status", "--porcelain"], cwd=cwd, capture_output=True,
                          text=True).stdout == ""
    _final, seen, _ = _drive(PLAN_CHAT, cwd, lambda last, n: "FINAL: Here is the plan again.")
    assert sum(s.startswith(CHECK_HEAD) for s in seen) == 1


def test_a_turn_that_edited_is_never_checked(cwd):
    final, seen, _ = _drive(
        [{"role": "user", "content": "add config.rs"}], cwd,
        lambda last, n: WRITE if n == 1 else "FINAL: Added config.rs.")
    assert final == "Added config.rs." and len(seen) == 2
    assert not any(s.startswith(CHECK_HEAD) for s in seen)


@pytest.mark.parametrize("mode", ["plan", "analyze"])
def test_read_only_modes_are_never_checked(cwd, mode):
    seen = []

    def fn(role, convo):
        seen.append(str(convo[-1].get("content")))
        return "FINAL: The plan: split main.rs."

    list(ca.run_chat_agent(PLAN_CHAT, cwd=str(cwd), complete_fn=fn, mode=mode))
    assert len(seen) == 1
