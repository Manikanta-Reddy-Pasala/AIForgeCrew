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
    assert evs[1]["type"] == "message" and "exit 0 in " in evs[1]["text"] and "hi" in evs[1]["text"]
    assert evs[1]["text"].split("exit 0 in ", 1)[1].startswith(f"{evs[0]['result']['elapsed_s']} s")
    assert isinstance(evs[0]["result"]["elapsed_s"], float)
    assert evs[-1] == {"type": "done"}
    assert pctx["done"] is True


def test_the_recipe_runs_in_the_chats_checkout_from_its_own_folder(tmp_path, monkeypatch):
    """The recipe is looked up under the project (``git_toplevel`` maps a chat
    worktree to its main repo) but RUNS in the checkout the chat works in,
    from the recipe's folder — never against the user's main checkout."""
    from aiforge_core.api.routes._chat import _routing as RT
    from aiforge_core.runtime import repo_ident
    from aiforge_core.runtime.tools import command_risk
    main, work = tmp_path / "main", tmp_path / "chat-1" / "work"
    (work / "web").mkdir(parents=True)
    monkeypatch.setattr(repo_ident, "git_toplevel", lambda cwd: str(main))
    R.save(str(main), "build", "make", cwd="web")
    monkeypatch.setattr(command_risk, "assess",
                        lambda cmd: {"level": command_risk.SAFE, "reason": ""})
    seen = {}
    monkeypatch.setattr(RT, "_run_recipe", lambda cmd, cwd: seen.update(cmd=cmd, cwd=cwd)
                        or {"cmd": cmd, "ok": True, "code": 0, "output": ""})
    evs = _drain(RT._recipe_pass("build it", str(work), 1, {"done": False}))
    exec_root = R.exec_root(str(work))
    assert seen == {"cmd": R.recipe_command({"cmd": "make", "cwd": "web"}, exec_root),
                    "cwd": exec_root}
    assert seen["cmd"].startswith("cd ") and seen["cmd"].endswith("/web && make")
    assert str(main) not in seen["cmd"] and seen["cwd"] != str(main)
    assert evs[0]["args"]["cmd"] == seen["cmd"]


def test_the_risk_check_sees_the_line_that_runs(tmp_path, monkeypatch):
    from aiforge_core.api.routes._chat import _routing as RT
    from aiforge_core.runtime.tools import command_risk
    (tmp_path / "web").mkdir()
    R.save(str(tmp_path), "build", "make", cwd="web")
    assessed = []
    monkeypatch.setattr(command_risk, "assess",
                        lambda cmd: assessed.append(cmd) or {"level": command_risk.SAFE, "reason": ""})
    monkeypatch.setattr(RT, "_run_recipe", lambda cmd, cwd: {"ok": True, "code": 0, "output": ""})
    _drain(RT._recipe_pass("build it", str(tmp_path), 1, {"done": False}))
    assert assessed == [R.recipe_command({"cmd": "make", "cwd": "web"}, R.exec_root(str(tmp_path)))]


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


def test_stop_reaches_a_running_recipe(tmp_path):
    """The route binds the session for the run (chat_cancel.set_active) so a
    Stop pressed while the recipe runs kills it and ends the turn — and the
    binding is restored afterwards."""
    from aiforge_core.api.routes._chat import _routing as RT
    from aiforge_core.runtime import chat_cancel
    R.save(str(tmp_path), "test", "sleep 5")
    sid = 777_001
    chat_cancel.start(sid)
    chat_cancel.cancel(sid)
    try:
        chat_cancel.set_active(None)
        pctx = {"done": False}
        evs = _drain(RT._recipe_pass("run tests", str(tmp_path), sid, pctx))
    finally:
        chat_cancel.finish(sid)
    assert evs[0]["type"] == "tool" and evs[0]["result"]["ok"] is False
    assert evs[1] == {"type": "error", "text": "stopped by user"} and evs[-1] == {"type": "done"}
    assert pctx["done"] is True and "prompt" not in pctx
    assert chat_cancel.active() is None


@pytest.mark.parametrize("verb,cmd,blocked", [
    ("run", "python -m http.server 8000", "server_start"),
    ("clean", "rm -rf build", "delete"),
])
def test_a_recipe_the_shell_gates_refuse_is_left_to_the_model(tmp_path, monkeypatch, verb, cmd, blocked):
    from aiforge_core.api.routes._chat import _routing as RT
    monkeypatch.delenv("AIFORGE_CHAT_ALLOW_DELETE", raising=False)
    monkeypatch.delenv("AIFORGE_ALLOW_DELETE", raising=False)
    R.save(str(tmp_path), verb, cmd)
    ran = []
    monkeypatch.setattr(RT, "_run_recipe", lambda c, cwd: ran.append(c) or {"ok": True, "code": 0})
    pctx = {"done": False}
    assert _drain(RT._recipe_pass(verb, str(tmp_path), 1, pctx)) == []
    assert ran == [] and pctx["done"] is False and "prompt" not in pctx
    assert pctx["note"] == f"recipe refused: {blocked}"


def test_a_steered_recipe_ends_the_turn_without_the_model(tmp_path, monkeypatch):
    from aiforge_core.api.routes._chat import _routing as RT
    from aiforge_core.runtime.run_interrupt import steered
    R.save(str(tmp_path), "test", "echo hi")
    monkeypatch.setattr(RT, "_run_recipe", lambda cmd, cwd: steered(cmd=cmd))
    pctx = {"done": False}
    evs = _drain(RT._recipe_pass("run tests", str(tmp_path), 1, pctx))
    assert evs[0]["type"] == "tool" and evs[-1] == {"type": "done"}
    assert evs[1]["type"] == "message" and "new message" in evs[1]["text"]
    assert pctx["done"] is True and "prompt" not in pctx


def test_a_recipe_that_could_not_start_says_so(tmp_path, monkeypatch):
    from aiforge_core.api.routes._chat import _routing as RT
    R.save(str(tmp_path), "test", "echo hi")
    monkeypatch.setattr(RT, "_run_recipe",
                        lambda cmd, cwd: {"cmd": cmd, "ok": False, "error": "No such file"})
    pctx = {"done": False}
    evs = _drain(RT._recipe_pass("run tests", str(tmp_path), 1, pctx))
    assert evs[0]["type"] == "tool" and evs[0]["result"]["code"] is None
    assert "could not start" in pctx["prompt"] and "No such file" in pctx["prompt"]
    assert "exit None" not in pctx["prompt"] and pctx["done"] is False


def test_the_run_uses_the_shell_tools_wall_clock(tmp_path, monkeypatch):
    from aiforge_core.api.routes._chat import _routing as RT
    from aiforge_core.runtime.tools import project_runner
    seen = []
    monkeypatch.setattr(project_runner, "_exec",
                        lambda cmd, cwd, timeout: seen.append(timeout) or {"ok": True, "code": 0, "output": ""})
    monkeypatch.setenv("AIFORGE_CHAT_CMD_TIMEOUT_S", "7")
    RT._run_recipe("echo hi", str(tmp_path))
    monkeypatch.delenv("AIFORGE_CHAT_CMD_TIMEOUT_S")
    RT._run_recipe("echo hi", str(tmp_path))
    assert seen == [7.0, RT._RECIPE_WALL_S]


def test_a_recipe_in_a_folder_is_safe_for_the_risk_check():
    from aiforge_core.runtime.tools import command_risk
    line = R.recipe_command({"cmd": "make", "cwd": "web"}, "/x")
    assert command_risk.assess(line)["level"] == command_risk.SAFE


def test_recipes_command_shares_the_lookup_root(tmp_path, monkeypatch):
    """``/recipes`` from a sub-folder (or a chat worktree) lists the project's
    store, resolved the same way as the route and the endpoints."""
    from aiforge_core.runtime import commands as C, repo_ident
    monkeypatch.setattr(repo_ident, "git_toplevel", lambda cwd: str(tmp_path))
    sub = tmp_path / "svc" / "api"
    sub.mkdir(parents=True)
    R.save(str(tmp_path), "test", "./t.sh")
    assert R.lookup_root(str(sub)) == str(tmp_path)
    assert "test → ./t.sh" in C.expand("/recipes", str(sub))


def test_a_failed_recipe_log_is_not_rule_classified(monkeypatch):
    """After a failing recipe hands its output to the agent, the producer
    neither classifies that log inline nor starts the background capture."""
    import types
    from aiforge_core.api.routes._chat import _capture_bg, _producer

    def _none(*_a, **_k):
        return iter(())
    for name in ("_early_route_events", "_prelude_notices", "_note_staleness_notice"):
        monkeypatch.setattr(_producer, name, _none)
    monkeypatch.setattr(_producer, "_warm_repo_map", lambda _cwd: None)
    monkeypatch.setattr(_producer._overlap, "warm_slots", lambda: None)

    def _fail(prompt, cwd, session_id, pctx):
        pctx["prompt"] = "The recipe `test` failed (exit 1; command: `pytest`).\n\nOutput:\n```\nboom\n```"
        yield {"type": "tool", "name": "run_command", "args": {}, "result": {"ok": False}}
    monkeypatch.setattr(_producer, "_recipe_pass", _fail)
    captured = []
    monkeypatch.setattr(_capture_bg, "inline_needed", lambda p: captured.append(("inline", p)) or True)
    monkeypatch.setattr(_capture_bg, "begin", lambda pc: captured.append(("bg", pc.prompt)))
    monkeypatch.setattr(_producer, "_rule_capture_pass",
                        lambda p, *a: captured.append(("rule", p)) or iter(()))
    stops = iter([False, True])
    monkeypatch.setattr(_producer, "_turn_was_stopped", lambda sid: next(stops))
    pc = types.SimpleNamespace(
        _cmd_help_text=None, body=None, history=[{"role": "user", "content": "run tests"}],
        cwd="/tmp", role="chat", session_id=778_001, _resume_brief="", _cmd_expanded=None,
        prompt="run tests", team=False, _wake=False)
    evs = list(_producer._events(pc))
    assert evs[0]["type"] == "tool" and evs[-1] == {"type": "done"}
    assert captured == []
    assert pc.prompt.startswith("The recipe `test` failed") and pc.history[-1]["content"] == pc.prompt


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


def test_the_route_skips_a_read_only_folder(monkeypatch):
    """A folder the user marked read-only is not built or run model-free."""
    from aiforge_core.api.routes._chat import _producer
    from aiforge_core.runtime import repo_roles
    monkeypatch.setattr(repo_roles, "role_of", lambda cwd, sid: (repo_roles.READ, cwd))
    assert _producer._read_only_folder("/src/lib", 5) is True
    monkeypatch.setattr(repo_roles, "role_of", lambda cwd, sid: (repo_roles.WORK, cwd))
    assert _producer._read_only_folder("/src/app", 5) is False
    monkeypatch.setattr(repo_roles, "role_of", lambda cwd, sid: None)
    assert _producer._read_only_folder("/src/app", 5) is False
