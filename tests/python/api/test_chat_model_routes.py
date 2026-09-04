"""The chat API's model picker, approval toggles, and session workspaces.

The model picker's hard problem is that the SAME model id can be registered
against two servers. Keying on the id alone hid the second registration
entirely — it could not be picked — and carrying the chat slot's current
base_url across a model change sent every request for the new model to the
previous model's host, which 404s an id it has never served. So rows are keyed
by (id, base_url) and a pick resolves the model's OWN endpoint.

The other rule with teeth: clearing a chat may delete only the MANAGED
`session-*` workspace under the chat root. A pinned project cwd, the root
itself, or a traversal escape is refused, so clearing a chat can never nuke a
real repo.
"""
from __future__ import annotations

import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aiforge_core.api.routes import chat as ch


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(ch.router)
    return TestClient(app)


@pytest.fixture(autouse=True)
def cfg(monkeypatch):
    rows = {"chat": {"provider": "openai_compatible", "model": "coder",
                     "base_url": "http://boxA:1234/v1", "insecure_tls": False},
            "planner": {"provider": "openai_compatible", "model": "thinker"},
            "_default": {"provider": "openai_compatible", "model": "coder"}}
    monkeypatch.setattr(ch._acfg, "archetypes",
                        lambda: ["chat", "planner", "_default"])
    monkeypatch.setattr(ch._acfg, "get", lambda role: rows.get(role, {}))
    monkeypatch.setattr(ch._acfg, "resolve_litellm",
                        lambda role: {"api_base": "http://boxA:1234/v1",
                                      "api_key": "sk-x", "insecure_tls": False})
    for var in ("AIFORGE_CHAT_MODEL", "AIFORGE__DEFAULT_MODEL",
                "AIFORGE_CHAT_QUICK_STEPS"):
        monkeypatch.delenv(var, raising=False)
    return rows


# ─── approval toggles ──────────────────────────────────────────────────


def test_the_three_modes_are_reported(client, monkeypatch):
    from aiforge_core.config import approval_settings
    monkeypatch.setattr(approval_settings, "all_modes",
                        lambda: {"simple": True, "plan": False, "team": True})
    assert client.get("/api/chat/approval-settings").json() == {
        "chat": True, "plan": False, "pipeline": True}


def test_a_mode_is_toggled(client, monkeypatch):
    from aiforge_core.config import approval_settings
    seen: dict = {}
    monkeypatch.setattr(approval_settings, "set_mode",
                        lambda mode, enabled: seen.update(mode=mode, on=enabled))
    monkeypatch.setattr(approval_settings, "all_modes",
                        lambda: {"simple": False, "plan": False, "team": False})
    client.put("/api/chat/approval-settings/plan", json={"enabled": True})
    assert seen == {"mode": "plan", "on": True}


def test_an_unknown_mode_is_a_400(client, monkeypatch):
    from aiforge_core.config import approval_settings
    monkeypatch.setattr(approval_settings, "set_mode",
                        lambda mode, enabled: (_ for _ in ()).throw(
                            ValueError("unknown mode")))
    r = client.put("/api/chat/approval-settings/nope", json={"enabled": True})
    assert r.status_code == 400
    assert "unknown mode" in r.json()["detail"]


# ─── the thin ask + the deprecated retain ──────────────────────────────


def test_the_thin_ask_proxies_one_completion(client, monkeypatch):
    from aiforge_core.orchestrator import llm_client
    seen: dict = {}

    def _call(role=None, system=None, user=None, temperature=None,
              max_tokens=None):
        seen.update(role=role, user=user)
        return "the answer"
    monkeypatch.setattr(llm_client, "call_text", _call)
    body = client.post("/api/chat/ask", json={"query": "  how?  "}).json()
    assert body == {"answer": "the answer", "trace": [], "hits": []}
    assert seen["user"] == "how?"
    assert seen["role"] == "doer"


def test_an_empty_ask_still_says_something(client, monkeypatch):
    from aiforge_core.orchestrator import llm_client
    monkeypatch.setattr(llm_client, "call_text", lambda **kw: "")
    assert client.post("/api/chat/ask",
                       json={"query": "  "}).json()["answer"] == "(empty response)"


def test_retain_is_a_no_op_that_accepts_any_body(client):
    """The typed annotation became an undefined forward-ref and 422'd the
    endpoint instead of returning the no-op."""
    r = client.post("/api/chat/retain", json={"anything": 1})
    assert r.status_code == 201
    assert r.json() == {"id": None, "retained": False, "reason": "deprecated"}


def test_the_default_cwd_prefers_the_workspace(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_WORKSPACE_DIR", str(tmp_path))
    assert ch._default_cwd() == str(tmp_path)
    monkeypatch.delenv("AIFORGE_WORKSPACE_DIR")
    monkeypatch.setenv("AIFORGE_REPO_ROOT", "/repo")
    assert ch._default_cwd() == "/repo"


# ─── discovering served models ─────────────────────────────────────────


def test_served_ids_come_from_the_provider_catalog(monkeypatch):
    monkeypatch.setattr(ch._acfg, "list_models",
                        lambda provider: [{"id": "a"}, {"id": None}, {}])
    assert ch._served_model_ids("local") == {"a"}


def test_an_undiscoverable_provider_serves_nothing(monkeypatch):
    monkeypatch.setattr(ch._acfg, "list_models",
                        lambda provider: (_ for _ in ()).throw(OSError("offline")))
    assert ch._served_model_ids("local") == set()


def test_an_openai_compatible_role_is_probed_directly(monkeypatch):
    """It has no static catalog — probe the role's own endpoint, exactly like
    the home-page Test."""
    import aiforge_core.llm.providers.openai_compatible as oc
    seen: dict = {}

    def _probe(base_url, api_key, insecure=False):
        seen.update(base_url=base_url, api_key=api_key)
        return {"models": ["coder", "thinker"]}
    monkeypatch.setattr(oc, "probe", _probe)
    assert ch._served_model_ids_for_role("chat") == {"coder", "thinker"}
    assert seen == {"base_url": "http://boxA:1234/v1", "api_key": "sk-x"}


def test_a_failed_probe_serves_nothing(monkeypatch):
    import aiforge_core.llm.providers.openai_compatible as oc
    monkeypatch.setattr(oc, "probe",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("refused")))
    assert ch._served_model_ids_for_role("chat") == set()


def test_a_catalog_provider_falls_back_to_provider_discovery(monkeypatch, cfg):
    cfg["chat"]["provider"] = "local"
    monkeypatch.setattr(ch._acfg, "list_models", lambda provider: [{"id": "local-1"}])
    assert ch._served_model_ids_for_role("chat") == {"local-1"}


# ─── merging the registry with what is served ──────────────────────────


@pytest.fixture
def registry(monkeypatch):
    from aiforge_core.config import model_registry
    rows: list = []
    monkeypatch.setattr(model_registry, "list_models", lambda: rows)
    return rows


def test_the_same_id_on_two_hosts_is_two_rows(registry):
    """Collapsing on the id hid the second registration completely — "use the
    copy on the other host" was not expressible."""
    registry.extend([{"model": "coder", "base_url": "http://boxA:1234/v1"},
                     {"model": "coder", "base_url": "http://boxB:1234/v1"}])
    out = ch._merge_registry_and_served({"coder"}, "http://boxA:1234/v1")
    assert [m["base_url"] for m in out] == ["http://boxA:1234/v1",
                                            "http://boxB:1234/v1"]
    assert [m["active"] for m in out] == [True, False]


def test_active_is_only_claimed_for_the_endpoint_that_answered(registry):
    """An id served by host A says nothing about the same id on host B."""
    registry.append({"model": "coder", "base_url": "http://boxB:1234/v1"})
    out = ch._merge_registry_and_served({"coder"}, "http://boxA:1234/v1")
    by_url = {m["base_url"]: m["active"] for m in out}
    assert by_url["http://boxB:1234/v1"] is False
    assert by_url["http://boxA:1234/v1"] is True


def test_a_url_less_registration_belongs_to_the_current_endpoint(registry):
    registry.append({"model": "coder", "base_url": ""})
    assert ch._merge_registry_and_served({"coder"}, "http://boxA/v1")[0]["active"] \
        is True


def test_embedding_models_are_never_offered(registry):
    registry.append({"model": "text-embed-bge", "base_url": ""})
    out = ch._merge_registry_and_served({"nomic-embed-text"}, "")
    assert out == []


def test_served_models_that_were_never_registered_still_appear(registry):
    out = ch._merge_registry_and_served({"loaded-only"}, "http://boxA/v1")
    assert out[0]["id"] == "loaded-only"
    assert out[0]["active"] is True


def test_active_models_sort_first(registry):
    registry.extend([{"model": "zzz", "base_url": ""},
                     {"model": "aaa", "base_url": "http://other/v1"}])
    out = ch._merge_registry_and_served({"zzz"}, "http://boxA/v1")
    assert out[0]["active"] is True
    assert out[0]["id"] == "zzz"
    assert out[-1]["id"] == "aaa"
    assert out[-1]["active"] is False


def test_an_unavailable_registry_leaves_the_served_list_standing(monkeypatch):
    from aiforge_core.config import model_registry
    monkeypatch.setattr(model_registry, "list_models",
                        lambda: (_ for _ in ()).throw(RuntimeError("no db")))
    assert ch._merge_registry_and_served({"coder"}, "u")[0]["id"] == "coder"


@pytest.mark.parametrize("a,b", [("http://x/v1/", "HTTP://X/v1"),
                                 (" http://x/v1 ", "http://x/v1"),
                                 (None, "")])
def test_urls_compare_case_and_slash_insensitively(a, b):
    assert ch._url_key(a) == ch._url_key(b)


# ─── the models endpoint ───────────────────────────────────────────────


def test_the_picker_lists_configured_and_served_models(client, registry,
                                                       monkeypatch):
    """Served-only would hide every model the user added but hasn't loaded."""
    registry.append({"model": "unloaded", "base_url": "http://boxA:1234/v1"})
    monkeypatch.setattr(ch, "_served_model_ids_for_role", lambda role: {"coder"})
    body = client.get("/api/chat/models").json()
    ids = [m["id"] for m in body["models"]]
    assert "unloaded" in ids
    assert "coder" in ids
    assert body["current"] == "coder"
    assert body["current_active"] is True
    assert body["current_base_url"] == "http://boxA:1234/v1"


def test_an_undiscoverable_endpoint_assumes_the_current_model_is_active(
        client, registry, monkeypatch):
    monkeypatch.setattr(ch, "_served_model_ids_for_role", lambda role: set())
    assert client.get("/api/chat/models").json()["current_active"] is True


def test_an_env_pin_is_surfaced_so_the_picker_can_warn(client, registry,
                                                       monkeypatch):
    """Otherwise the picker silently saves a model that never runs."""
    monkeypatch.setenv("AIFORGE_CHAT_MODEL", "pinned-model")
    monkeypatch.setattr(ch, "_served_model_ids_for_role", lambda role: set())
    body = client.get("/api/chat/models").json()
    assert body["env_override"] == {"var": "AIFORGE_CHAT_MODEL",
                                    "model": "pinned-model"}


def test_no_env_pin_reports_none(monkeypatch):
    monkeypatch.delenv("AIFORGE_CHAT_MODEL", raising=False)
    assert ch._model_env_override("chat") is None


# ─── picking a model ───────────────────────────────────────────────────


def test_the_models_own_endpoint_wins(monkeypatch):
    """Carrying the slot's current base_url across a model change sent every
    request to the previous model's host."""
    monkeypatch.setattr(ch._model_registry, "connection_for",
                        lambda model, want: {"base_url": "http://boxB:1234/v1",
                                             "api_key": "sk-b",
                                             "insecure_tls": True})
    assert ch._endpoint_for_picked_model("coder", {"base_url": "http://boxA/v1"}) \
        == ("http://boxB:1234/v1", "sk-b", True)


def test_an_explicit_pick_stands_when_the_registry_cannot_confirm_it(monkeypatch):
    monkeypatch.setattr(ch._model_registry, "connection_for",
                        lambda model, want: None)
    base, key, tls = ch._endpoint_for_picked_model(
        "coder", {"base_url": "http://boxA/v1", "insecure_tls": True},
        "http://boxC/v1")
    assert base == "http://boxC/v1"
    assert key is None
    assert tls is True


def test_with_nothing_to_go_on_the_slots_connection_is_kept(monkeypatch):
    monkeypatch.setattr(ch._model_registry, "connection_for",
                        lambda model, want: None)
    assert ch._endpoint_for_picked_model("coder", {"base_url": "http://boxA/v1"})[0] \
        == "http://boxA/v1"


@pytest.fixture
def save(monkeypatch):
    saved: list = []

    def _set_role(role, provider, model, base_url=None, api_key=None,
                  insecure_tls=False):
        saved.append({"role": role, "provider": provider, "model": model,
                      "base_url": base_url, "api_key": api_key,
                      "insecure_tls": insecure_tls})
        return {"provider": provider, "model": model}
    monkeypatch.setattr(ch._acfg, "set_role", _set_role)
    monkeypatch.setattr(ch._model_registry, "connection_for",
                        lambda model, want: {"base_url": "http://boxB/v1"})
    monkeypatch.setattr(ch, "_served_model_ids_for_role", lambda role: {"new-model"})
    from aiforge_core.runtime import vision_detect
    monkeypatch.setattr(vision_detect, "reset_vision_cache", lambda: None)
    monkeypatch.setattr(vision_detect, "warm_vision_async", lambda role: None)
    return saved


def test_a_pick_applies_to_every_agent_by_default(client, save):
    """Electing a bigger model should change TEAM mode too, not just chat."""
    body = client.put("/api/chat/model", json={"model": "new-model"}).json()
    assert [s["role"] for s in save] == ["chat", "_default"]
    assert body["applied_to"] == "all agents"
    assert body["active"] is True


def test_a_pick_can_be_scoped_to_chat_only(client, save):
    body = client.put("/api/chat/model",
                      json={"model": "new-model", "apply_all": False}).json()
    assert [s["role"] for s in save] == ["chat"]
    assert body["applied_to"] == "chat only"


def test_an_inactive_model_is_saved_but_flagged(client, save, monkeypatch):
    monkeypatch.setattr(ch, "_served_model_ids_for_role", lambda role: {"other"})
    assert client.put("/api/chat/model",
                      json={"model": "new-model"}).json()["active"] is False


def test_a_rejected_pick_is_a_400(client, save, monkeypatch):
    monkeypatch.setattr(ch._acfg, "set_role",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError("bad url")))
    r = client.put("/api/chat/model", json={"model": "m"})
    assert r.status_code == 400
    assert "bad url" in r.json()["detail"]


def test_an_env_pinned_slot_warns_that_the_pick_will_not_apply(client, save,
                                                               monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_MODEL", "pinned")
    body = client.put("/api/chat/model", json={"model": "new-model"}).json()
    assert "overrides it" in body["warning"]
    assert body["env_override"]["model"] == "pinned"


def test_a_pin_matching_the_pick_is_not_a_warning(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_MODEL", "same")
    env, warning = ch._env_pin_warning({"model": "same"}, apply_all=False)
    assert warning is None
    assert env["model"] == "same"


def test_the_vision_capability_is_re_identified_after_a_pick(client, save,
                                                             monkeypatch):
    from aiforge_core.runtime import vision_detect
    calls: list = []
    monkeypatch.setattr(vision_detect, "reset_vision_cache",
                        lambda: calls.append("reset"))
    monkeypatch.setattr(vision_detect, "warm_vision_async",
                        lambda role: calls.append(role))
    client.put("/api/chat/model", json={"model": "new-model"})
    assert calls == ["reset", "chat"]


def test_a_broken_vision_probe_does_not_fail_the_pick(client, save, monkeypatch):
    from aiforge_core.runtime import vision_detect
    monkeypatch.setattr(vision_detect, "reset_vision_cache",
                        lambda: (_ for _ in ()).throw(RuntimeError("no db")))
    assert client.put("/api/chat/model",
                      json={"model": "new-model"}).status_code == 200


# ─── reloading at a chosen context window ──────────────────────────────


@pytest.fixture
def reload_env(monkeypatch):
    from aiforge_core.runtime import local_starter
    from aiforge_core.runtime import vision_detect
    state = {"result": {"ok": True, "model": "coder", "ctx": 65536}}
    monkeypatch.setattr(local_starter, "load_model_now",
                        lambda model, ctx, ttl=0: state["result"])
    monkeypatch.setattr(vision_detect, "reset_vision_cache", lambda: None)
    monkeypatch.setattr(vision_detect, "warm_vision_async", lambda role: None)
    return state


def test_a_model_is_reloaded_at_the_requested_window(client, reload_env):
    body = client.post("/api/chat/model/reload",
                       json={"model": "coder", "context_length": 65536}).json()
    assert body["ctx"] == 65536


def test_no_lms_host_is_a_503(client, reload_env):
    reload_env["result"] = {"ok": False, "error": "AIFORGE_LMS_HOST not set"}
    assert client.post("/api/chat/model/reload",
                       json={"model": "c", "context_length": 4096}).status_code == 503


def test_a_failed_load_is_a_502(client, reload_env):
    reload_env["result"] = {"ok": False, "error": "ssh: connection refused"}
    assert client.post("/api/chat/model/reload",
                       json={"model": "c", "context_length": 4096}).status_code == 502


def test_an_out_of_range_context_length_is_rejected(client, reload_env):
    assert client.post("/api/chat/model/reload",
                       json={"model": "c", "context_length": 10}).status_code == 422


# ─── the orchestrator slot ─────────────────────────────────────────────


def test_the_orchestrator_picks_from_the_chat_model_universe(client, monkeypatch):
    """Probing the planner's own base_url would empty the dropdown when it is a
    per-model proxy that serves no /v1/models list."""
    monkeypatch.setattr(ch, "_served_model_ids_for_role", lambda role: {"coder"})
    body = client.get("/api/chat/orchestrator-model").json()
    assert body["model"] == "thinker"
    assert {m["id"] for m in body["models"]} == {"coder", "thinker"}
    assert body["roles"] == ["enhancer", "architect", "planner"]


def test_the_orchestrator_model_is_set_on_every_orchestrator_role(client,
                                                                  monkeypatch):
    saved: list = []
    monkeypatch.setattr(ch._acfg, "set_role",
                        lambda role, provider, model, base_url=None,
                        insecure_tls=False: saved.append((role, model, base_url)))
    body = client.put("/api/chat/orchestrator-model",
                      json={"model": "thinker-2"}).json()
    assert body == {"ok": True, "model": "thinker-2",
                    "roles": ["enhancer", "architect", "planner"]}
    assert [r for r, _m, _u in saved] == ["enhancer", "architect", "planner"]
    # the CHAT slot's endpoint, not the role's own stale per-model proxy
    assert {u for _r, _m, u in saved} == {"http://boxA:1234/v1"}


def test_a_rejected_orchestrator_pick_is_a_400(client, monkeypatch):
    monkeypatch.setattr(ch._acfg, "set_role",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError("nope")))
    assert client.put("/api/chat/orchestrator-model",
                      json={"model": "m"}).status_code == 400


# ─── managed session workspaces ────────────────────────────────────────


@pytest.fixture
def ws_root(monkeypatch, tmp_path):
    root = tmp_path / "chat-workspaces"
    root.mkdir()
    monkeypatch.setenv("AIFORGE_CHAT_WORKSPACE_ROOT", str(root))
    return root


def test_a_managed_session_workspace_is_deleted(ws_root):
    d = ws_root / "session-7"
    d.mkdir()
    (d / "file.txt").write_text("x")
    assert ch._delete_chat_workspace(str(d)) is True
    assert not d.exists()


@pytest.mark.parametrize("target", ["", None, "   "])
def test_an_empty_cwd_deletes_nothing(ws_root, target):
    assert ch._delete_chat_workspace(target) is False


def test_a_pinned_project_repo_is_never_deleted(ws_root, tmp_path):
    """Clearing a chat must not be able to nuke a real repo."""
    repo = tmp_path / "my-repo"
    repo.mkdir()
    assert ch._delete_chat_workspace(str(repo)) is False
    assert repo.exists()


def test_the_workspace_root_itself_is_never_deleted(ws_root):
    assert ch._delete_chat_workspace(str(ws_root)) is False
    assert ws_root.exists()


def test_a_non_session_directory_inside_the_root_is_kept(ws_root):
    d = ws_root / "shared-cache"
    d.mkdir()
    assert ch._delete_chat_workspace(str(d)) is False
    assert d.exists()


def test_a_traversal_escape_is_refused(ws_root, tmp_path):
    outside = tmp_path / "session-evil"
    outside.mkdir()
    assert ch._delete_chat_workspace(str(ws_root / ".." / "session-evil")) is False
    assert outside.exists()


def test_an_isolated_workspace_is_recognised(ws_root):
    d = ws_root / "session-7"
    d.mkdir()
    assert ch._is_isolated_workspace(str(d)) is True
    assert ch._is_isolated_workspace(str(ws_root)) is False
    assert ch._is_isolated_workspace("/some/repo") is False
    assert ch._is_isolated_workspace("") is False


def test_orphaned_session_dirs_are_swept(ws_root):
    (ws_root / "session-1").mkdir()
    (ws_root / "session-2").mkdir()
    (ws_root / "keepme").mkdir()
    assert ch._sweep_orphan_session_dirs() == 2
    assert (ws_root / "keepme").exists()


def test_a_missing_workspace_root_sweeps_nothing(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CHAT_WORKSPACE_ROOT", str(tmp_path / "gone"))
    assert ch._sweep_orphan_session_dirs() == 0


def test_the_workspace_root_defaults_under_the_config_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("AIFORGE_CHAT_WORKSPACE_ROOT", raising=False)
    monkeypatch.setattr(ch, "config_dir", lambda: str(tmp_path))
    assert ch._chat_workspace_root() == os.path.join(str(tmp_path),
                                                     "chat-workspaces")
