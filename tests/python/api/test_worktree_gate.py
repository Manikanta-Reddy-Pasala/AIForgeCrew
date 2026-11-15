"""Turn start with fakes: the once-per-project question, replaying the held
request, the old-worktree list as a notice or inside the question, and the
deterministic replies to it. Plus the prefs API."""
from __future__ import annotations

import json
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.python.runtime.worktree_ask_fixtures import age, git, wenv  # noqa: F401


class _Run:
    def __init__(self):
        self.events = []

    def publish(self, ev):
        self.events.append(ev)


class _Body:
    def __init__(self, content, mode="simple"):
        self.content, self.mode = content, mode


def _pc(env, sid, prompt):
    """A turn as _message builds it: the user's row already stored."""
    env.store.add_message(sid, "user", prompt)
    hist = [{"role": r["role"], "content": r["content"]}
            for r in env.store.get_messages(sid)]
    pc = type("PC", (), {})()
    pc.session_id, pc.body, pc.run = sid, _Body(prompt), _Run()
    pc.cwd, pc.prompt, pc.history = str(env.repo), prompt, hist
    return pc


def _turn(env, sid, prompt, monkeypatch):
    from aiforge_core.api.routes._chat import _producer as pr
    from aiforge_core.runtime import chat_runs
    monkeypatch.setattr(chat_runs, "set_phase", lambda *a, **k: None)
    pc = _pc(env, sid, prompt)
    pr._ensure_chat_worktree(pc)
    ctx = {"done": False}
    evs = list(pr._worktree_gate_events(pc, ctx))
    msg = next((e for e in evs if e.get("type") == "message"), None)
    if msg:                                  # what _finalize persists
        env.store.add_message(sid, "assistant", msg["text"])
    return pc, evs, ctx["done"], msg


def _notes(evs):
    return "\n".join(e["text"] for e in evs if e.get("type") == "thought")


def _dead(env, name="TAL-50", days=30):
    d = os.path.join(str(env.repo), ".aiforge-worktrees", name)
    os.makedirs(d)
    open(os.path.join(d, "left.txt"), "w").write("z\n")
    age(d, days)
    return d


def test_first_chat_asks_once_then_replays_the_request(wenv, monkeypatch):
    sid = wenv.new_chat()
    pc, evs, done, msg = _turn(wenv, sid, "add a discount to src/cart.py", monkeypatch)
    assert done and msg["awaiting_input"]
    assert f"This project ({wenv.real}) has no worktree setting yet." in msg["text"]
    assert wenv.store.get_session(sid).get("workdir") is None      # nothing made yet
    assert pc.cwd == str(wenv.repo)
    pend = json.loads(open(wenv.cfg / "worktree_pending.json").read())
    assert pend[str(sid)]["ask"]["prompt"] == "add a discount to src/cart.py"

    pc, evs, done, msg = _turn(wenv, sid, "2", monkeypatch)
    assert not done and msg is None                       # the agent runs this turn
    assert pc.prompt == "add a discount to src/cart.py" and pc.body.content == pc.prompt
    assert pc.history == [{"role": "user", "content": "add a discount to src/cart.py"}]
    from aiforge_core.runtime import chat_worktree, worktree_prefs
    assert worktree_prefs.get(wenv.real)["place"] == "config"
    wd = wenv.store.get_session(sid)["workdir"]
    assert pc.cwd == wd and wd.startswith(chat_worktree.config_holder(wenv.real) + os.sep)
    notes = _notes(evs)
    assert f"This chat works in {wd} on branch `aiforge/chat-{sid}-" in notes
    assert "PUT /api/chat/worktree-prefs" in notes and "Now running your request" in notes
    assert worktree_prefs.session_state(sid).get("ask") is None

    # the next chat in this project is not asked again
    sid2 = wenv.new_chat()
    pc2, evs2, done2, msg2 = _turn(wenv, sid2, "fix the total", monkeypatch)
    assert not done2 and msg2 is None
    assert wenv.store.get_session(sid2)["workdir"].startswith(chat_worktree.config_holder(wenv.real))


def test_choice_none_runs_in_the_project_folder(wenv, monkeypatch):
    sid = wenv.new_chat()
    _turn(wenv, sid, "rename total", monkeypatch)
    pc, evs, done, _ = _turn(wenv, sid, "no worktree", monkeypatch)
    assert not done and pc.prompt == "rename total" and pc.cwd == str(wenv.repo)
    assert wenv.store.get_session(sid).get("workdir") is None
    from aiforge_core.runtime import worktree_prefs
    assert worktree_prefs.get(wenv.real)["place"] == "none"


def test_a_reply_that_is_not_a_choice_is_a_new_request_with_the_default(wenv, monkeypatch):
    sid = wenv.new_chat()
    _turn(wenv, sid, "rename total", monkeypatch)
    pc, evs, done, _ = _turn(wenv, sid, "actually, explain what cart.py does first", monkeypatch)
    assert not done and pc.prompt == "actually, explain what cart.py does first"
    assert "nothing was saved" in _notes(evs)
    from aiforge_core.runtime import worktree_prefs
    assert worktree_prefs.get(wenv.real) is None
    wd = wenv.store.get_session(sid)["workdir"]
    assert wd.startswith(os.path.join(wenv.real, ".aiforge-worktrees") + os.sep)


def test_env_place_means_no_question(wenv, monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_WORKTREE_PLACE", "repo")
    sid = wenv.new_chat()
    pc, evs, done, msg = _turn(wenv, sid, "fix it", monkeypatch)
    assert not done and msg is None and wenv.store.get_session(sid)["workdir"]


def test_old_list_is_a_non_blocking_notice_once_and_keep_snoozes(wenv, monkeypatch):
    from aiforge_core.runtime import worktree_prefs
    worktree_prefs.set_place(wenv.real, "repo")
    dead = _dead(wenv)
    sid = wenv.new_chat()
    pc, evs, done, msg = _turn(wenv, sid, "fix it", monkeypatch)
    assert not done and msg is None                       # the request still runs
    notes = _notes(evs)
    assert "Old AIForge worktrees in this project (1;" in notes and dead in notes
    assert wenv.store.get_session(sid)["workdir"]
    # same session: not shown again
    _, evs, _, _ = _turn(wenv, sid, "and the tests", monkeypatch)
    assert "Old AIForge worktrees" not in _notes(evs)
    # "keep worktrees": no agent, and no new chat lists the same set for 7 days
    _, evs, done, msg = _turn(wenv, sid, "keep worktrees", monkeypatch)
    assert done and "I won't list them again for 7 days" in msg["text"]
    assert os.path.isdir(dead)
    sid2 = wenv.new_chat()
    _, evs2, _, _ = _turn(wenv, sid2, "fix it", monkeypatch)
    assert "Old AIForge worktrees" not in _notes(evs2)


def test_delete_reply_moves_dead_folder_to_trash_without_the_agent(wenv, monkeypatch):
    from aiforge_core.runtime import worktree_prefs
    worktree_prefs.set_place(wenv.real, "repo")
    dead = _dead(wenv)
    sid = wenv.new_chat()
    _turn(wenv, sid, "fix it", monkeypatch)
    _, evs, done, msg = _turn(wenv, sid, "delete worktrees 1", monkeypatch)
    assert done and not msg.get("awaiting_input")
    assert "TAL-50: dead folder moved to " + str(wenv.cfg / "worktree-trash") in msg["text"]
    assert not os.path.exists(dead)


def test_question_and_list_in_one_message_and_one_reply_answers_both(wenv, monkeypatch):
    dead = _dead(wenv)
    sid = wenv.new_chat()
    _, _, done, msg = _turn(wenv, sid, "add logging", monkeypatch)
    assert done and msg["awaiting_input"]
    assert "has no worktree setting yet" in msg["text"] and "1. TAL-50" in msg["text"]
    pc, evs, done, _ = _turn(wenv, sid, "1, delete worktrees all", monkeypatch)
    assert not done and pc.prompt == "add logging"
    assert not os.path.exists(dead)
    from aiforge_core.runtime import worktree_prefs
    assert worktree_prefs.get(wenv.real)["place"] == "repo"
    assert "dead folder moved to" in _notes(evs)


def test_only_the_cleanup_answered_asks_the_question_again(wenv, monkeypatch):
    _dead(wenv)
    sid = wenv.new_chat()
    _turn(wenv, sid, "add logging", monkeypatch)
    _, _, done, msg = _turn(wenv, sid, "keep worktrees", monkeypatch)
    assert done and msg["awaiting_input"]
    assert "Kept all 1 old worktree" in msg["text"] and "has no worktree setting yet" in msg["text"]
    pc, _, done, _ = _turn(wenv, sid, "3", monkeypatch)
    assert not done and pc.prompt == "add logging"
    assert pc.history == [{"role": "user", "content": "add logging"}]


def test_a_failure_in_the_gate_never_blocks_the_turn(wenv, monkeypatch):
    from aiforge_core.runtime import worktree_prefs
    monkeypatch.setattr(worktree_prefs, "session_state",
                        lambda sid: (_ for _ in ()).throw(RuntimeError("boom")))
    sid = wenv.new_chat()
    pc, evs, done, msg = _turn(wenv, sid, "fix it", monkeypatch)
    assert not done and msg is None and pc.cwd == str(wenv.repo)


def test_prefs_api(wenv):
    from aiforge_core.api.routes import chat as ch
    app = FastAPI()
    app.include_router(ch.router)
    c = TestClient(app)
    r = c.get("/api/chat/worktree-prefs", params={"repo": str(wenv.repo)})
    assert r.status_code == 200 and r.json()["pref"] is None and r.json()["effective"] is None
    r = c.put("/api/chat/worktree-prefs", json={"repo": str(wenv.repo), "place": "config"})
    assert r.status_code == 200 and r.json()["pref"]["place"] == "config"
    assert c.get("/api/chat/worktree-prefs").json()["prefs"][wenv.real]["place"] == "config"
    assert c.put("/api/chat/worktree-prefs",
                 json={"repo": str(wenv.repo), "place": "moon"}).status_code == 400
    assert c.put("/api/chat/worktree-prefs",
                 json={"repo": "relative/path", "place": "repo"}).status_code == 400
    r = c.delete("/api/chat/worktree-prefs", params={"repo": str(wenv.repo)})
    assert r.json()["cleared"] is True
    _dead(wenv)
    r = c.get("/api/chat/worktrees/old", params={"repo": str(wenv.repo)})
    assert [e["name"] for e in r.json()["old"]] == ["TAL-50"]
