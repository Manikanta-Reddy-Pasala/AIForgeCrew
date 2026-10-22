"""The zero-LLM recipe route: a recipe sentence is saved without the agent, a
bare verb phrase runs the recipe and answers with its output, a failing run is
handed to the agent, a risky recipe is left to the approval gate; plus the
``/recipes`` built-in and the per-session recipe endpoints."""
import importlib
import pytest
from fastapi.testclient import TestClient
from aiforge_core.runtime import recipes as R


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_RECIPES", "1")


def _drain(gen):
    return list(gen)


def test_a_recipe_sentence_is_saved_without_the_agent(tmp_path):
    from aiforge_core.api.routes._chat import _routing as RT
    pctx = {"done": False}
    evs = _drain(RT._recipe_pass("remember: compile = ./build.sh", str(tmp_path), 1, pctx))
    assert evs[0]["type"] == "message" and "build" in evs[0]["text"] and "./build.sh" in evs[0]["text"]
    assert evs[-1] == {"type": "done"} and pctx["done"] is True
    assert R.get(str(tmp_path), "build")["cmd"] == "./build.sh"


def test_a_remove_sentence_drops_the_recipe(tmp_path):
    from aiforge_core.api.routes._chat import _routing as RT
    R.save(str(tmp_path), "test", "./t.sh")
    pctx = {"done": False}
    evs = _drain(RT._recipe_pass("forget the test recipe", str(tmp_path), 1, pctx))
    assert evs[0]["type"] == "message" and "Removed" in evs[0]["text"]
    assert pctx["done"] is True and R.get(str(tmp_path), "test") is None
    pctx = {"done": False}
    evs = _drain(RT._recipe_pass("forget the test recipe", str(tmp_path), 1, pctx))
    assert "no `test` recipe" in evs[0]["text"] and pctx["done"] is True


def test_a_bare_phrase_runs_the_recipe_without_the_agent(tmp_path, monkeypatch):
    from aiforge_core.api.routes._chat import _routing as RT
    R.save(str(tmp_path), "test", "echo hi")
    seen = {}
    monkeypatch.setattr(RT, "_run_recipe", lambda cmd, cwd: seen.update(cmd=cmd, cwd=cwd)
                        or {"cmd": cmd, "ok": True, "code": 0, "output": "hi\n"})
    pctx = {"done": False}
    evs = _drain(RT._recipe_pass("run tests", str(tmp_path), 1, pctx))
    assert seen == {"cmd": "echo hi", "cwd": str(tmp_path)}
    assert evs[0]["type"] == "tool" and evs[0]["name"] == "run_command"
    assert evs[1]["type"] == "message" and "exit 0" in evs[1]["text"] and "hi" in evs[1]["text"]
    assert evs[-1] == {"type": "done"}
    assert pctx["done"] is True


def test_the_recipe_runs_in_its_own_cwd(tmp_path, monkeypatch):
    from aiforge_core.api.routes._chat import _routing as RT
    R.save(str(tmp_path), "build", "make", cwd="web")
    seen = {}
    monkeypatch.setattr(RT, "_run_recipe", lambda cmd, cwd: seen.update(cmd=cmd, cwd=cwd)
                        or {"cmd": cmd, "ok": True, "code": 0, "output": ""})
    _drain(RT._recipe_pass("build it", str(tmp_path), 1, {"done": False}))
    assert seen == {"cmd": "make", "cwd": str(tmp_path / "web")}


def test_a_failing_recipe_hands_the_output_to_the_agent(tmp_path, monkeypatch):
    from aiforge_core.api.routes._chat import _routing as RT
    R.save(str(tmp_path), "test", "false")
    monkeypatch.setattr(RT, "_run_recipe", lambda cmd, cwd: {"cmd": cmd, "ok": False, "code": 1, "output": "boom"})
    pctx = {"done": False}
    evs = _drain(RT._recipe_pass("run tests", str(tmp_path), 1, pctx))
    assert pctx["done"] is False
    assert evs[0]["type"] == "tool" and evs[0]["result"]["ok"] is False
    assert len(evs) == 1
    assert "recipe `test` failed" in pctx["prompt"] and "boom" in pctx["prompt"]


def test_a_risky_recipe_is_left_to_the_approval_gate(tmp_path, monkeypatch):
    """A recipe whose command the risk check does not clear as safe never runs
    on the zero-LLM route: the turn goes to the model (and its approval gate)
    with the prompt unchanged, and the producer gets a note."""
    from aiforge_core.api.routes._chat import _routing as RT
    from aiforge_core.runtime.tools import command_risk
    R.save(str(tmp_path), "deploy", "sudo ./deploy.sh")
    monkeypatch.setattr(command_risk, "assess",
                        lambda cmd: {"level": command_risk.CAUTION, "reason": "sudo"})
    ran = []
    monkeypatch.setattr(RT, "_run_recipe", lambda cmd, cwd: ran.append(cmd) or {"ok": True})
    pctx = {"done": False}
    assert _drain(RT._recipe_pass("deploy", str(tmp_path), 1, pctx)) == []
    assert ran == []
    assert pctx["done"] is False and "prompt" not in pctx
    assert pctx["note"] == "recipe needs approval"


def test_other_messages_pass_through(tmp_path):
    from aiforge_core.api.routes._chat import _routing as RT
    pctx = {"done": False}
    assert _drain(RT._recipe_pass("fix the login bug", str(tmp_path), 1, pctx)) == []
    assert pctx["done"] is False


def test_the_route_is_off_with_the_switch(tmp_path, monkeypatch):
    from aiforge_core.api.routes._chat import _routing as RT
    monkeypatch.setenv("AIFORGE_CHAT_RECIPES", "0")
    pctx = {"done": False}
    assert _drain(RT._recipe_pass("remember: compile = ./build.sh", str(tmp_path), 1, pctx)) == []
    assert pctx["done"] is False


def test_recipes_slash_command_lists_them(tmp_path):
    from aiforge_core.runtime import commands as C
    R.save(str(tmp_path), "test", "./t.sh")
    assert C.is_builtin("recipes")
    assert "test → ./t.sh" in C.expand("/recipes", str(tmp_path))
    assert "/recipes" in C.expand("/help", str(tmp_path))


def test_producer_persists_recipe_offer_steps():
    from aiforge_core.api.routes._chat import _turn_events as TE
    st, steps = {"subtasks": []}, []
    TE._route_produce_event({"type": "recipe_offer", "offers": [{"verb": "test", "cmd": "pytest"}]},
                            st, steps)
    assert steps and steps[0]["type"] == "recipe_offer"


def _fresh_app(monkeypatch, tmp_path):
    monkeypatch.delenv("AIFORGE_PG_URL", raising=False)
    monkeypatch.delenv("AIFORGE_API_TOKEN", raising=False)
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_CHAT_DB_PATH", str(tmp_path / "chat.db"))
    import aiforge_core.api.api as api
    importlib.reload(api)
    from aiforge_core.runtime import chat_store
    chat_store.reset_backend_for_tests()
    return api


def test_recipe_endpoints_save_list_decline_remove(monkeypatch, tmp_path):
    api = _fresh_app(monkeypatch, tmp_path)
    from aiforge_core.runtime import chat_store
    repo = tmp_path / "repo"
    repo.mkdir()
    sid = chat_store.create_session("hello", role="chat", cwd=str(repo))["id"]
    client = TestClient(api.app)
    r = client.post(f"/api/chat/sessions/{sid}/recipes", json={"verb": "test", "cmd": "./t.sh"})
    assert r.status_code == 200 and r.json()["ok"]
    assert client.get(f"/api/chat/sessions/{sid}/recipes").json()["recipes"]["test"]["cmd"] == "./t.sh"
    assert client.post(f"/api/chat/sessions/{sid}/recipes/decline", json={"verb": "build"}).json()["ok"]
    assert client.delete(f"/api/chat/sessions/{sid}/recipes/test").json()["ok"]
    assert client.get(f"/api/chat/sessions/{sid}/recipes").json()["recipes"] == {}
    assert client.post("/api/chat/sessions/9999/recipes", json={"verb": "x", "cmd": "y"}).status_code == 404


def test_recipe_endpoint_canonicalises_the_verb_and_rejects_bad_input(monkeypatch, tmp_path):
    api = _fresh_app(monkeypatch, tmp_path)
    from aiforge_core.runtime import chat_store
    repo = tmp_path / "repo"
    repo.mkdir()
    sid = chat_store.create_session("hello", role="chat", cwd=str(repo))["id"]
    client = TestClient(api.app)
    r = client.post(f"/api/chat/sessions/{sid}/recipes", json={"verb": "compile", "cmd": "./build.sh"})
    assert r.status_code == 200 and r.json()["verb"] == "build"
    assert R.get(str(repo), "build")["cmd"] == "./build.sh"
    assert client.post(f"/api/chat/sessions/{sid}/recipes", json={"verb": "x", "cmd": "  "}).status_code == 400
    assert client.post(f"/api/chat/sessions/{sid}/recipes",
                       json={"verb": "x", "cmd": "y", "cwd": "../out"}).status_code == 400
    assert client.delete(f"/api/chat/sessions/{sid}/recipes/nothing").json()["ok"] is False
