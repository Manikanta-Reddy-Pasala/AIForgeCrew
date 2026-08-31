"""The post-turn learner for simple/plan chat.

The full pipeline runs a Learner node after a PASS verdict; single chat never
did, so work only reached long-term memory if the agent happened to call
memory_write itself. This module closes that gap, and two of its rules exist
because a local model is unreliable in specific ways.

First: when the turn actually CHANGED the repo, the task-done record is
authored from GROUND TRUTH (the edit tools that succeeded) rather than trusted
to the model, which routinely emits a generic fact instead. Second: when the
distiller is down, an explicit "remember this" instruction still persists —
otherwise a stated preference is silently lost to a flaky endpoint.
"""
from __future__ import annotations

import pytest

from aiforge_core.runtime import chat_learner as cl


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.delenv("AIFORGE_CHAT_LEARNER", raising=False)
    monkeypatch.delenv("AIFORGE_CHAT_LEARNER_MAX_TOKENS", raising=False)
    monkeypatch.delenv("AIFORGE_CHAT_LEARNER_TIMEOUT_S", raising=False)


def _tool(name, ok=True, path=None):
    return {"type": "tool", "name": name, "args": {"path": path} if path else {},
            "result": {"ok": ok, **({"path": path} if path else {})}}


# ─── switches ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("flag", ["0", "false", "no"])
def test_the_learner_can_be_turned_off(monkeypatch, flag):
    monkeypatch.setenv("AIFORGE_CHAT_LEARNER", flag)
    assert cl.learn_from_chat(prompt="p", final_text="a", steps=None,
                              repo="r", session_id=1) == {"ok": False,
                                                          "skipped": "disabled"}


@pytest.mark.parametrize("raw,expected", [("50", 50), ("junk", 800), (None, 800)])
def test_int_env_falls_back(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("AIFORGE_X", raising=False)
    else:
        monkeypatch.setenv("AIFORGE_X", raw)
    assert cl._int_env("AIFORGE_X", 800) == expected


# ─── the transcript ────────────────────────────────────────────────────


def test_the_transcript_carries_ask_actions_and_answer():
    out = cl._transcript("fix the parser", "done", [_tool("file_write", path="a.py"),
                                                    {"type": "thought"}])
    assert "USER:\nfix the parser" in out
    assert "ACTIONS:\n- file_write ok=True" in out
    assert "ASSISTANT:\ndone" in out


def test_a_turn_with_no_tools_has_no_actions_block():
    assert "ACTIONS" not in cl._transcript("hi", "hello", [])


def test_the_transcript_is_capped():
    assert len(cl._transcript("x" * 20000, "y", None)) == 8000


def test_only_the_first_forty_actions_are_listed():
    out = cl._transcript("p", "a", [_tool(f"t{i}") for i in range(60)])
    assert out.count("ok=True") == 40


# ─── parsing the reply ─────────────────────────────────────────────────


@pytest.mark.parametrize("raw,expected", [
    ('[{"text": "a"}]', '[{"text": "a"}]'),
    ('```json\n[{"text": "a"}]\n```', '[{"text": "a"}]'),
    ('```\n[{"text": "a"}]\n```', '[{"text": "a"}]'),
    ('Here you go:\n[{"text": "a"}]\nhope that helps', '[{"text": "a"}]'),
    ("no array here", "[]"),
    ("", "[]"),
    ("][", "[]"),
])
def test_the_json_array_is_recovered_from_a_wrapped_reply(raw, expected):
    assert cl._extract_json(raw) == expected


# ─── ground truth ──────────────────────────────────────────────────────


def test_only_successful_edit_tools_count_as_changes(monkeypatch):
    import aiforge_core.runtime.text_doer as td
    monkeypatch.setattr(td, "_EDIT_TOOLS", {"file_write", "file_patch"})
    steps = [_tool("file_write", path="a.py"),
             _tool("file_patch", ok=False, path="b.py"),
             _tool("file_read", path="c.py"),
             _tool("file_write", path="a.py")]          # duplicate
    assert cl._changed_files(steps) == ["a.py"]


def test_a_turn_with_no_steps_changed_nothing():
    assert cl._changed_files(None) == []


@pytest.mark.parametrize("fact,is_solution", [
    ({"kind": "feature"}, True),
    ({"kind": "FIX"}, True),
    ({"text": "DID: shipped the cache"}, True),
    ({"topic": "task-history"}, True),
    ({"text": "prefers tabs", "kind": "preference"}, False),
    ("not a dict", False),
])
def test_which_facts_already_declare_a_completed_task(fact, is_solution):
    assert cl._is_solution_fact(fact) is is_solution


def test_a_synthesized_record_describes_what_changed():
    out = cl._synthesize_solution_fact(
        "add an LRU cache", [{"text": "added an LRU cache to the store"}],
        ["app/store.py", "app/cli.py"])
    assert out["text"] == "DID: added an LRU cache to the store"
    assert out["topic"] == "task-history" and out["kind"] == "feature"
    assert out["about"] == ["store.py", "cli.py"]
    assert out["files"] == ["app/store.py", "app/cli.py"]


@pytest.mark.parametrize("prompt,kind", [
    ("fix the crash", "fix"), ("the parser is broken", "fix"),
    ("tests fail on main", "fix"), ("add a cache", "feature"),
])
def test_the_record_kind_comes_from_the_ask(prompt, kind):
    assert cl._synthesize_solution_fact(prompt, [], ["a.py"])["kind"] == kind


def test_the_summary_falls_back_to_the_user_request():
    out = cl._synthesize_solution_fact("add a cache", [{"text": "  "}], ["a.py"])
    assert out["text"] == "DID: add a cache"


# ─── the end-to-end turn ───────────────────────────────────────────────


@pytest.fixture()
def learner(monkeypatch):
    """Stub the LLM + the persistence layer."""
    from aiforge_core.llm import client
    from aiforge_core.runtime import learner_persist
    import aiforge_core.runtime.text_doer as td
    monkeypatch.setattr(td, "_EDIT_TOOLS", {"file_write"})
    state: dict = {"reply": "[]", "persisted": [], "raise": None}

    def _complete(role, messages, **kw):
        state["role"] = role
        state["messages"] = messages
        state["kw"] = kw
        if state["raise"]:
            raise state["raise"]
        return state["reply"]
    monkeypatch.setattr(client, "complete", _complete)
    monkeypatch.setattr(learner_persist, "_coerce_facts",
                        lambda raw: __import__("json").loads(raw))

    def _persist(facts, repo=None, session_id=None, event_time=None):
        state["persisted"].append({"facts": facts, "repo": repo,
                                   "session_id": session_id})
        return {"written_observations": len(facts), "written_decisions": 0}
    monkeypatch.setattr(learner_persist, "persist_facts", _persist)
    return state


def test_an_empty_turn_is_skipped(learner):
    assert cl.learn_from_chat(prompt="", final_text="a", steps=None, repo="r",
                              session_id=1)["skipped"] == "empty"
    assert cl.learn_from_chat(prompt="p", final_text="", steps=None, repo="r",
                              session_id=1)["skipped"] == "empty"


def test_distilled_facts_are_persisted(learner):
    learner["reply"] = '[{"text": "the store is an LRU", "topic": "arch"}]'
    out = cl.learn_from_chat(prompt="how does the store work?",
                             final_text="it is an LRU", steps=None,
                             repo="app", session_id=7)
    assert out == {"ok": True, "written_observations": 1, "written_decisions": 0}
    assert learner["persisted"][0]["repo"] == "app"
    assert learner["persisted"][0]["session_id"] == "7"
    assert learner["role"] == "learner"


def test_a_turn_worth_nothing_persists_nothing(learner):
    out = cl.learn_from_chat(prompt="hi", final_text="hello", steps=None,
                             repo="app", session_id=1)
    assert out == {"ok": True, "written_observations": 0,
                   "written_decisions": 0, "llm_down": False}
    assert learner["persisted"] == []


def test_a_repo_change_always_lands_a_task_done_record(learner):
    """A local model routinely emits a generic fact instead of the DID: line —
    the changelog would silently lose the work."""
    learner["reply"] = '[{"text": "the code is structured in modules"}]'
    cl.learn_from_chat(prompt="add an LRU cache", final_text="done",
                       steps=[_tool("file_write", path="app/store.py")],
                       repo="app", session_id=1)
    facts = learner["persisted"][0]["facts"]
    assert facts[-1]["topic"] == "task-history"
    assert facts[-1]["files"] == ["app/store.py"]


def test_a_self_declared_record_is_not_duplicated(learner):
    learner["reply"] = '[{"text": "DID: added the cache", "topic": "task-history"}]'
    cl.learn_from_chat(prompt="add a cache", final_text="done",
                       steps=[_tool("file_write", path="a.py")],
                       repo="app", session_id=1)
    facts = learner["persisted"][0]["facts"]
    assert len([f for f in facts if f.get("topic") == "task-history"]) == 1


def test_a_read_only_turn_synthesizes_nothing(learner):
    learner["reply"] = '[{"text": "the store is an LRU"}]'
    cl.learn_from_chat(prompt="explain the store", final_text="it is an LRU",
                       steps=[_tool("file_read", path="a.py")], repo="app",
                       session_id=1)
    assert all(f.get("topic") != "task-history"
               for f in learner["persisted"][0]["facts"])


def test_an_explicit_instruction_survives_a_dead_distiller(learner):
    """Otherwise a stated preference is lost to a flaky endpoint."""
    learner["raise"] = RuntimeError("endpoint down")
    out = cl.learn_from_chat(prompt="always squash merges on this repo",
                             final_text="noted", steps=None, repo="app",
                             session_id=1)
    assert out["llm_down"] is True
    assert learner["persisted"][0]["facts"] == [
        {"text": "always squash merges on this repo", "tags": ["preference"]}]


def test_ordinary_chatter_is_not_saved_when_the_distiller_is_down(learner):
    learner["raise"] = RuntimeError("endpoint down")
    cl.learn_from_chat(prompt="what does this function do?", final_text="x",
                       steps=None, repo="app", session_id=1)
    assert learner["persisted"] == []


def test_a_failed_fallback_write_is_not_fatal(learner, monkeypatch):
    from aiforge_core.runtime import learner_persist
    learner["raise"] = RuntimeError("endpoint down")
    monkeypatch.setattr(learner_persist, "persist_facts",
                        lambda **kw: (_ for _ in ()).throw(OSError("disk full")))
    assert cl.learn_from_chat(prompt="always squash", final_text="ok",
                              steps=None, repo="app", session_id=1)["ok"] is True


def test_a_failed_persist_is_reported(learner, monkeypatch):
    from aiforge_core.runtime import learner_persist
    learner["reply"] = '[{"text": "a fact"}]'
    monkeypatch.setattr(learner_persist, "persist_facts",
                        lambda **kw: (_ for _ in ()).throw(OSError("db locked")))
    out = cl.learn_from_chat(prompt="p", final_text="a", steps=None,
                             repo="app", session_id=1)
    assert out == {"ok": False, "error": "db locked"}


def test_the_repo_falls_back_to_the_configured_one(learner, monkeypatch):
    monkeypatch.setenv("AIFORGE_AFM_REPO", "fallback-repo")
    learner["reply"] = '[{"text": "a"}]'
    cl.learn_from_chat(prompt="p", final_text="a", steps=None, repo="",
                       session_id=1)
    assert learner["persisted"][0]["repo"] == "fallback-repo"


def test_the_learner_prompt_asks_for_the_task_done_record(learner):
    learner["reply"] = "[]"
    cl.learn_from_chat(prompt="p", final_text="a", steps=None, repo="r",
                       session_id=1)
    user = learner["messages"][1]["content"]
    assert "task-history" in user and "DID:" in user
    assert "USER:\np" in user


def test_the_learner_call_is_bounded(learner, monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_LEARNER_MAX_TOKENS", "128")
    monkeypatch.setenv("AIFORGE_CHAT_LEARNER_TIMEOUT_S", "5")
    cl.learn_from_chat(prompt="p", final_text="a", steps=None, repo="r",
                       session_id=1)
    assert learner["kw"]["max_tokens"] == 128
    assert learner["kw"]["timeout_s"] == 5
    assert learner["kw"]["temperature"] == 0.0
