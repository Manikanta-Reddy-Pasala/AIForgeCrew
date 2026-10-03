"""'Yes, continue' means do the work. Live: every 'continue' / 'do it' produced
'Nothing was written this turn … say start and I will do step 1 next turn'."""
import subprocess

import pytest

from aiforge_core.runtime.chat_agent._turn import _goahead as G


@pytest.mark.parametrize("text,yes", [
    ("yes continue", True), ("continue", True), ("do it", True), ("Yes, go ahead", True),
    ("ok proceed", True), ("start", True), ("yes use new branch", True),
    ("yes, but what about the tests?", False), ("why did it fail?", False),
    ("rewrite the poller into config.rs and read.rs", False), ("", False),
])
def test_is_go_ahead(text, yes):
    assert G.is_go_ahead(text) is yes


@pytest.mark.parametrize("text,yes", [
    ("Say **start** and I will do step 1 in the next turn with real file writes.", True),
    ("Shall I proceed with the rewrite?", True),
    ("Do you want me to start with the Rust poller?", True),
    ("Reply yes and I'll begin.", True),
    ("I changed config.rs and ran cargo check; it is clean.", False),
])
def test_asks_permission(text, yes):
    assert G.asks_permission(text) is yes


@pytest.mark.parametrize("text,yes", [
    ("Nothing was written this turn. I only re-verified state.", True),
    ("The production rewrite has still not been executed.", True),
    ("What actually needs to happen (next turns, one module at a time):", True),
    ("Wrote config.rs and read.rs; cargo check is clean.", False),
])
def test_admits_no_work(text, yes):
    assert G.admits_no_work(text) is yes


def _repo(tmp_path):
    run = lambda *a: subprocess.run(["git", *a], cwd=tmp_path, capture_output=True, check=True)
    run("init", "-q"); run("config", "user.email", "t@t"); run("config", "user.name", "t")
    (tmp_path / "main.rs").write_text("fn main() {}\n")
    run("add", "-A"); run("commit", "-q", "-m", "init")
    return str(tmp_path)


HISTORY = [
    {"role": "user", "content": "rewrite the poller into config.rs, read.rs and publish.rs"},
    {"role": "assistant", "content": "Plan: 1. split main.rs. 2. add read.rs. "
                                     "Say start and I will do step 1."},
    {"role": "user", "content": "yes continue"},
]
STALL = ("Nothing was written this turn. I only re-verified state.\n\n"
         "Say **start** and I will do step 1 in the next turn with real file writes.")


def test_a_stalled_final_after_yes_continue_is_sent_back_and_the_work_is_done(tmp_path):
    from aiforge_core.runtime import chat_agent as ca
    cwd = _repo(tmp_path)
    state = {"n": 0}

    def fn(role, convo):
        state["n"] += 1
        last = str(convo[-1].get("content"))
        if "ALREADY told you to go ahead" in last:
            return ('ACTION: file_write\nARGS_JSON: {"path": "config.rs", '
                    '"content": "pub struct Config;\\n"}')
        if "OBSERVATION" in last and "config.rs" in last:
            return "FINAL: Wrote config.rs (step 1). cargo check is clean."
        return "FINAL: " + STALL

    evs = list(ca.run_chat_agent(HISTORY, cwd=cwd, complete_fn=fn))
    msgs = [e["text"] for e in evs if e.get("type") == "message"]
    assert (tmp_path / "config.rs").exists()
    assert msgs and "Wrote config.rs" in msgs[-1]
    assert "(No file was changed" not in msgs[-1]


def test_a_model_that_never_acts_is_nudged_three_times_then_labelled_not_looped(tmp_path):
    from aiforge_core.runtime import chat_agent as ca
    cwd = _repo(tmp_path)
    nudges = {"n": 0}

    def fn(role, convo):
        if "ALREADY told you to go ahead" in str(convo[-1].get("content")):
            nudges["n"] += 1
        return "FINAL: " + STALL

    evs = list(ca.run_chat_agent(HISTORY, cwd=cwd, complete_fn=fn))
    msgs = [e["text"] for e in evs if e.get("type") == "message"]
    assert nudges["n"] == 3
    assert msgs and msgs[-1].startswith("(No file was changed")
    assert not (tmp_path / "config.rs").exists()


def test_a_fresh_question_is_not_treated_as_a_go_ahead(tmp_path):
    from aiforge_core.runtime import chat_agent as ca
    cwd = _repo(tmp_path)
    evs = list(ca.run_chat_agent(
        [{"role": "user", "content": "what does main.rs do?"}], cwd=cwd,
        complete_fn=lambda r, c: "FINAL: It is an empty main()."))
    msgs = [e["text"] for e in evs if e.get("type") == "message"]
    assert msgs == ["It is an empty main()."]
