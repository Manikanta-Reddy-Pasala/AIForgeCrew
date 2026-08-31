"""Agent config, the model registry, and the provider connectivity tests.

Two themes run through these endpoints. First, the SECRET: the UI never echoes
a stored API key back into its field, so every read path reports only whether
one is set, and every Test path fills the blank from the saved config —
otherwise "Test" right after "Save" sends no token and 401s.

Second, the registry is the thing that decides which model each agent uses, so
a change to it re-runs capability assignment and invalidates the per-model
probe caches. Both are best-effort and must never break the mutation that
triggered them.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aiforge_core.api.routes import agents as ag


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(ag.router)
    return TestClient(app)


@pytest.fixture(autouse=True)
def cfg(monkeypatch):
    """A small, deterministic agent_config."""
    rows = {
        "doer": {"provider": "openai_compatible", "model": "coder",
                 "base_url": "http://box:1234/v1", "api_key": "sk-secret",
                 "insecure_tls": True},
        "planner": {"provider": "openai_compatible", "model": "thinker"},
        "_default": {"provider": "openai_compatible", "model": "coder"},
    }
    monkeypatch.setattr(ag._acfg, "load_all", lambda: dict(rows))
    monkeypatch.setattr(ag._acfg, "archetypes", lambda: ["doer", "planner"])
    monkeypatch.setattr(ag._acfg, "get", lambda role: rows.get(role, {}))
    monkeypatch.setattr(ag._acfg, "list_providers",
                        lambda: [{"id": "openai_compatible", "label": "OpenAI-compatible",
                                  "default_model": "coder"}])
    monkeypatch.setattr(ag._acfg, "resolve_litellm",
                        lambda role: {"api_base": "http://box:1234/v1",
                                      "api_key": "sk-secret", "model": "coder",
                                      "insecure_tls": False})
    return rows


# ─── the role catalogue ────────────────────────────────────────────────


@pytest.mark.parametrize("role,group", [
    ("chat", "chat"),
    ("planner", "orchestrator"),
    ("enhancer", "orchestrator"),
    ("architect", "orchestrator"),
    ("ctx_memory", "fanout"),
    ("verify_scope", "fanout"),
    ("doer", "pipeline"),
    ("nosuchrole", "pipeline"),
])
def test_roles_are_grouped_for_the_page(role, group):
    assert ag._role_group(role) == group


def test_the_synthetic_default_is_hidden(monkeypatch):
    monkeypatch.setattr(ag._acfg, "archetypes", lambda: ["doer", "_default"])
    assert ag._visible_roles() == ["doer"]


def test_a_broken_config_falls_back_to_the_legacy_roles(monkeypatch):
    monkeypatch.setattr(ag._acfg, "archetypes",
                        lambda: (_ for _ in ()).throw(RuntimeError("bad yaml")))
    assert ag._visible_roles()          # non-empty: the page still renders


def test_a_row_carries_the_model_and_description(client):
    rows = client.get("/api/agents").json()
    doer = next(r for r in rows if r["role"] == "doer")
    assert doer["model"] == "coder"
    assert doer["description"].startswith("Writes the actual code")
    assert doer["group"] == "pipeline"


def test_a_row_for_an_unconfigurable_role_still_renders(monkeypatch):
    monkeypatch.setattr(ag._acfg, "get",
                        lambda role: (_ for _ in ()).throw(RuntimeError("no row")))
    row = ag._agent_row("doer")
    assert row["role"] == "doer" and row["model"]        # from the legacy ROLES
    row2 = ag._agent_row("ctx_memory")                    # no legacy row either
    assert row2["transport"] == "openai_compatible"


def test_activity_is_null_on_the_embedded_backend():
    """The rollup was Postgres-only; the catalogue must still render."""
    assert ag._role_activity("doer") == (None, 0, [])


# ─── v1 + v2 config reads ──────────────────────────────────────────────


def test_the_v1_config_lists_providers_and_roles(client, monkeypatch):
    monkeypatch.setattr(ag._acfg, "_ARCHETYPES", ["doer", "planner"])
    body = client.get("/api/config/agents").json()
    assert body["archetype_order"] == ["doer", "planner"]
    assert body["providers"]["openai_compatible"]["default_model"] == "coder"
    assert body["roles"]["doer"]["model"] == "coder"


def test_the_v2_config_never_echoes_the_key(client):
    body = client.get("/api/agents/v2/config").json()
    assert body["doer"]["api_key_set"] is True
    assert "sk-secret" not in str(body)
    assert body["doer"]["insecure_tls"] is True
    assert body["planner"]["api_key_set"] is False


def test_providers_carry_their_models(client, monkeypatch):
    monkeypatch.setattr(ag._acfg, "list_models", lambda pid: ["coder", "thinker"])
    assert client.get("/api/agents/v2/providers").json()[0]["models"] == \
        ["coder", "thinker"]


def test_a_provider_whose_models_cannot_be_listed_still_appears(client, monkeypatch):
    monkeypatch.setattr(ag._acfg, "list_models",
                        lambda pid: (_ for _ in ()).throw(OSError("offline")))
    assert client.get("/api/agents/v2/providers").json()[0]["models"] == []


# ─── saved-credential fallback ─────────────────────────────────────────


def test_blank_fields_fall_back_to_the_saved_config():
    """Without this, Test right after Save sends no token and 401s."""
    assert ag._saved_role_credentials("doer", "", None, False) == (
        "http://box:1234/v1", "sk-secret", False)


def test_explicit_values_win_over_the_saved_ones():
    assert ag._saved_role_credentials("doer", "http://other/v1", "sk-typed", True) == (
        "http://other/v1", "sk-typed", True)


def test_an_unknown_role_has_nothing_to_borrow():
    assert ag._saved_role_credentials("nosuch", "", None, False) == ("", None, False)


def test_a_placeholder_key_is_not_a_key(monkeypatch):
    monkeypatch.setattr(ag._acfg, "resolve_litellm",
                        lambda role: {"api_base": "http://box/v1", "api_key": "not-needed"})
    assert ag._saved_role_credentials("doer", "", None, False)[1] is None


def test_a_saved_insecure_flag_is_inherited(monkeypatch):
    monkeypatch.setattr(ag._acfg, "resolve_litellm",
                        lambda role: {"api_base": "http://box/v1", "insecure_tls": True})
    assert ag._saved_role_credentials("doer", "", None, False)[2] is True


def test_a_broken_resolve_leaves_the_fields_alone(monkeypatch):
    monkeypatch.setattr(ag._acfg, "resolve_litellm",
                        lambda role: (_ for _ in ()).throw(RuntimeError("bad row")))
    assert ag._saved_role_credentials("doer", "", None, False) == ("", None, False)


def test_a_key_is_recovered_from_whichever_role_shares_the_url():
    """Keys live on ROLE rows, not model rows, so a per-model test button has
    to find one that points at the same endpoint."""
    assert ag._key_for_base_url("http://box:1234/v1/") == "sk-secret"


def test_no_role_matching_the_url_means_no_key(monkeypatch):
    assert ag._key_for_base_url("http://elsewhere/v1") is None


def test_a_broken_role_list_yields_no_key(monkeypatch):
    monkeypatch.setattr(ag._acfg, "archetypes",
                        lambda: (_ for _ in ()).throw(RuntimeError("bad")))
    assert ag._key_for_base_url("http://box:1234/v1") is None


def test_a_role_that_cannot_resolve_is_skipped(monkeypatch):
    monkeypatch.setattr(ag._acfg, "archetypes", lambda: ["broken", "doer"])
    calls: list = []

    def _resolve(role):
        calls.append(role)
        if role == "broken":
            raise RuntimeError("bad row")
        return {"api_base": "http://box:1234/v1", "api_key": "sk-secret"}
    monkeypatch.setattr(ag._acfg, "resolve_litellm", _resolve)
    assert ag._key_for_base_url("http://box:1234/v1") == "sk-secret"
    assert calls == ["broken", "doer"]


# ─── connectivity probes ───────────────────────────────────────────────


def test_the_probe_gets_the_saved_credentials(client, monkeypatch):
    import aiforge_core.llm.providers.openai_compatible as oc
    seen: dict = {}

    def _probe(base_url, api_key, insecure=False):
        seen.update(base_url=base_url, api_key=api_key, insecure=insecure)
        return {"ok": True, "models": ["coder"]}
    monkeypatch.setattr(oc, "probe", _probe)
    body = client.post("/api/providers/test", json={"role": "doer"}).json()
    assert body == {"ok": True, "models": ["coder"]}
    assert seen == {"base_url": "http://box:1234/v1", "api_key": "sk-secret",
                    "insecure": False}


def test_the_native_probe_falls_back_to_the_saved_model(client, monkeypatch):
    import aiforge_core.llm.providers.openai_compatible as oc
    seen: dict = {}

    def _probe_native(base_url, model, api_key, insecure=False):
        seen.update(base_url=base_url, model=model, api_key=api_key)
        return {"ok": True, "native": True}
    monkeypatch.setattr(oc, "probe_native", _probe_native)
    assert client.post("/api/providers/test-native",
                       json={"role": "doer"}).json()["native"] is True
    assert seen["model"] == "coder"


def test_a_per_model_test_recovers_the_key_from_the_matching_role(client, monkeypatch):
    import aiforge_core.llm.providers.openai_compatible as oc
    seen: dict = {}
    monkeypatch.setattr(oc, "probe_native",
                        lambda base_url, model, api_key, insecure=False:
                        seen.update(api_key=api_key) or {"ok": True})
    client.post("/api/providers/test-native",
                json={"base_url": "http://box:1234/v1", "model": "coder"})
    assert seen["api_key"] == "sk-secret"


def test_a_role_whose_model_cannot_be_resolved_probes_with_none(client, monkeypatch):
    import aiforge_core.llm.providers.openai_compatible as oc
    monkeypatch.setattr(ag._acfg, "resolve_litellm",
                        lambda role: (_ for _ in ()).throw(RuntimeError("bad row")))
    seen: dict = {}
    monkeypatch.setattr(oc, "probe_native",
                        lambda base_url, model, api_key, insecure=False:
                        seen.update(model=model) or {"ok": False})
    client.post("/api/providers/test-native", json={"role": "doer"})
    assert seen["model"] == ""


# ─── writing config ────────────────────────────────────────────────────


def test_a_role_is_saved(client, monkeypatch):
    monkeypatch.setattr(ag._acfg, "set_role",
                        lambda role, provider, model: {"provider": provider,
                                                       "model": model})
    body = client.put("/api/config/agents/doer",
                      json={"provider": "openai_compatible", "model": "new"}).json()
    assert body == {"role": "doer", "provider": "openai_compatible", "model": "new"}


def test_an_invalid_role_save_is_a_400(client, monkeypatch):
    monkeypatch.setattr(ag._acfg, "set_role",
                        lambda *a: (_ for _ in ()).throw(ValueError("unknown provider")))
    r = client.put("/api/config/agents/doer",
                   json={"provider": "nope", "model": "m"})
    assert r.status_code == 400 and "unknown provider" in r.json()["detail"]


@pytest.fixture()
def v2_save(monkeypatch):
    monkeypatch.setattr(ag._acfg, "PROVIDERS", {"openai_compatible": {}})
    monkeypatch.setattr(ag._acfg, "_DEFAULT_KEY", "_default")
    seen: dict = {}

    def _set_role(role, provider, model, base_url=None, api_key=None,
                  insecure_tls=False):
        seen.update(role=role, provider=provider, model=model,
                    base_url=base_url, api_key=api_key, insecure_tls=insecure_tls)
        return {"provider": provider, "model": model, "base_url": base_url,
                "api_key": api_key, "insecure_tls": insecure_tls}
    monkeypatch.setattr(ag._acfg, "set_role", _set_role)
    return seen


def test_the_v2_save_reports_only_that_a_key_is_set(client, v2_save):
    body = client.put("/api/agents/v2/doer/config",
                      json={"provider": "openai_compatible", "model": "m",
                            "base_url": " http://box/v1 ", "api_key": " sk-x ",
                            "insecure_tls": True}).json()
    assert body["api_key_set"] is True and "sk-x" not in str(body)
    assert v2_save["base_url"] == "http://box/v1"      # trimmed
    assert v2_save["api_key"] == "sk-x"


def test_the_global_default_row_is_writable(client, v2_save):
    r = client.put("/api/agents/v2/_default/config",
                   json={"provider": "openai_compatible", "model": "m"})
    assert r.status_code == 200 and v2_save["role"] == "_default"


def test_an_unknown_archetype_is_a_404(client, v2_save):
    r = client.put("/api/agents/v2/nosuch/config",
                   json={"provider": "openai_compatible", "model": "m"})
    assert r.status_code == 404


def test_an_unknown_provider_is_a_400(client, v2_save):
    r = client.put("/api/agents/v2/doer/config",
                   json={"provider": "nope", "model": "m"})
    assert r.status_code == 400 and "unknown provider" in r.json()["detail"]


def test_an_empty_model_is_a_400(client, v2_save):
    r = client.put("/api/agents/v2/doer/config",
                   json={"provider": "openai_compatible", "model": "   "})
    assert r.status_code == 400


def test_a_rejected_save_is_a_400(client, v2_save, monkeypatch):
    monkeypatch.setattr(ag._acfg, "set_role",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError("bad base_url")))
    r = client.put("/api/agents/v2/doer/config",
                   json={"provider": "openai_compatible", "model": "m"})
    assert r.status_code == 400


# ─── the model registry ────────────────────────────────────────────────


@pytest.fixture()
def registry(monkeypatch):
    from aiforge_core.config import model_registry
    state: dict = {"rows": [{"id": "m1", "model": "coder"}], "assigned": []}
    monkeypatch.setattr(model_registry, "list_models", lambda: state["rows"])
    monkeypatch.setattr(model_registry, "add_model",
                        lambda **kw: state.setdefault("added", kw) or {"id": "m2", **kw})
    monkeypatch.setattr(model_registry, "update_model",
                        lambda mid, **kw: {"id": mid, **kw} if mid == "m1" else None)
    monkeypatch.setattr(model_registry, "remove_model", lambda mid: mid == "m1")
    monkeypatch.setattr(model_registry, "sync_from_config", lambda: {"added": 2})
    monkeypatch.setattr(model_registry, "auto_assign",
                        lambda roles: state["assigned"].append(list(roles)) or {"doer": "m1"})
    monkeypatch.setattr(model_registry, "suggest_assignments",
                        lambda roles: {r: "m1" for r in roles})
    monkeypatch.setattr(model_registry, "apply_to_roles",
                        lambda mid, roles: {"model": mid, "roles": roles})
    monkeypatch.setenv("AIFORGE_VISION_PROBE_ON_ADD", "0")
    monkeypatch.delenv("AIFORGE_AUTO_ASSIGN_AGENTS", raising=False)
    return state


def test_models_are_listed(client, registry):
    assert client.get("/api/agents/models").json() == {"models": [{"id": "m1",
                                                                   "model": "coder"}]}


def test_adding_a_model_reassigns_the_agents(client, registry):
    r = client.post("/api/agents/models", json={"model": "new-coder"})
    assert r.status_code == 201
    assert registry["added"]["model"] == "new-coder"
    assert registry["assigned"] == [["doer", "planner"]]


def test_a_model_id_is_required(client, registry):
    assert client.post("/api/agents/models", json={"label": "x"}).status_code == 400


def test_a_rejected_model_is_a_400(client, registry, monkeypatch):
    from aiforge_core.config import model_registry
    monkeypatch.setattr(model_registry, "add_model",
                        lambda **kw: (_ for _ in ()).throw(ValueError("duplicate")))
    r = client.post("/api/agents/models", json={"model": "dup"})
    assert r.status_code == 400 and "duplicate" in r.json()["detail"]


def test_auto_assign_can_be_turned_off(client, registry, monkeypatch):
    monkeypatch.setenv("AIFORGE_AUTO_ASSIGN_AGENTS", "0")
    client.post("/api/agents/models", json={"model": "new"})
    assert registry["assigned"] == []


def test_a_failed_reassignment_never_breaks_the_mutation(client, registry, monkeypatch):
    from aiforge_core.config import model_registry
    monkeypatch.setattr(model_registry, "auto_assign",
                        lambda roles: (_ for _ in ()).throw(RuntimeError("no models")))
    assert client.post("/api/agents/models", json={"model": "new"}).status_code == 201


def test_a_vision_probe_is_started_in_the_background(client, registry, monkeypatch):
    monkeypatch.setenv("AIFORGE_VISION_PROBE_ON_ADD", "1")
    started: list = []
    import threading

    class _T:
        def __init__(self, **kw):
            started.append(kw["args"])

        def start(self):
            pass
    monkeypatch.setattr(threading, "Thread", _T)
    client.post("/api/agents/models", json={"model": "new", "base_url": "http://x"})
    assert started and started[0][1] == "new"


def test_a_broken_probe_launch_never_breaks_model_add(client, registry, monkeypatch):
    monkeypatch.setenv("AIFORGE_VISION_PROBE_ON_ADD", "1")
    import threading
    monkeypatch.setattr(threading, "Thread",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("no threads")))
    assert client.post("/api/agents/models", json={"model": "new"}).status_code == 201


def test_updating_a_model_clears_the_probe_caches(client, registry, monkeypatch):
    from aiforge_core.runtime import chat_media
    from aiforge_core.runtime.chat_agent import _native
    cleared: list = []
    monkeypatch.setattr(chat_media, "reset_vision_cache", lambda: cleared.append("vision"))
    monkeypatch.setattr(_native, "reset_native_cache", lambda: cleared.append("native"))
    assert client.put("/api/agents/models/m1", json={"model": "v2"}).status_code == 200
    assert cleared == ["vision", "native"]


def test_updating_a_missing_model_is_a_404(client, registry):
    assert client.put("/api/agents/models/nope", json={"model": "x"}).status_code == 404


def test_a_cache_reset_failure_does_not_fail_the_update(client, registry, monkeypatch):
    from aiforge_core.runtime import chat_media
    monkeypatch.setattr(chat_media, "reset_vision_cache",
                        lambda: (_ for _ in ()).throw(RuntimeError("no cache")))
    assert client.put("/api/agents/models/m1", json={"model": "v2"}).status_code == 200


def test_deleting_a_model_reassigns(client, registry):
    assert client.delete("/api/agents/models/m1").status_code == 204
    assert registry["assigned"] == [["doer", "planner"]]


def test_deleting_a_missing_model_is_a_404(client, registry):
    assert client.delete("/api/agents/models/nope").status_code == 404


def test_syncing_from_config_reassigns(client, registry):
    assert client.post("/api/agents/models/sync").json() == {"added": 2}
    assert registry["assigned"] == [["doer", "planner"]]


def test_a_model_is_applied_to_roles(client, registry):
    body = client.post("/api/agents/models/m1/apply",
                       json={"roles": ["doer"]}).json()
    assert body == {"model": "m1", "roles": ["doer"]}


def test_applying_a_missing_model_is_a_404(client, registry, monkeypatch):
    from aiforge_core.config import model_registry
    monkeypatch.setattr(model_registry, "apply_to_roles",
                        lambda mid, roles: (_ for _ in ()).throw(ValueError("no model")))
    assert client.post("/api/agents/models/nope/apply",
                       json={"roles": []}).status_code == 404


# ─── capability auto-assign ────────────────────────────────────────────


def test_the_preview_applies_nothing(client, registry):
    body = client.get("/api/agents/auto-assign").json()
    assert body["assignments"] == {"doer": "m1", "planner": "m1"}
    assert registry["assigned"] == []


def test_a_dry_run_applies_nothing(client, registry):
    body = client.post("/api/agents/auto-assign", json={"dry_run": True}).json()
    assert body["applied"] is False and registry["assigned"] == []


def test_auto_assign_applies_to_every_archetype_by_default(client, registry):
    assert client.post("/api/agents/auto-assign", json={}).json()["applied"] is True
    assert registry["assigned"] == [["doer", "planner"]]


def test_auto_assign_can_be_scoped_to_named_roles(client, registry):
    client.post("/api/agents/auto-assign", json={"roles": ["doer"]})
    assert registry["assigned"] == [["doer"]]


# ─── profiles + reset ──────────────────────────────────────────────────


def test_profiles_are_listed(client, monkeypatch):
    monkeypatch.setattr(ag._acfg, "PROFILES",
                        {"local": {"provider": "openai_compatible", "model": "coder"}})
    assert client.get("/api/agents/v2/profiles").json()["profiles"] == [
        {"name": "local", "provider": "openai_compatible", "model": "coder"}]


def test_applying_a_profile(client, monkeypatch):
    monkeypatch.setattr(ag._acfg, "apply_profile",
                        lambda name: {"doer": {"model": "coder"}})
    body = client.put("/api/agents/v2/profile/local").json()
    assert body["profile"] == "local" and body["roles"]["doer"]["model"] == "coder"


def test_an_unknown_profile_is_a_404(client, monkeypatch):
    monkeypatch.setattr(ag._acfg, "apply_profile",
                        lambda name: (_ for _ in ()).throw(ValueError("no profile")))
    assert client.put("/api/agents/v2/profile/nope").status_code == 404


def test_reset_clears_the_per_role_rows(client, monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(ag._acfg, "reset",
                        lambda keep_default=False: seen.setdefault("keep", keep_default) or {"ok": True})
    client.post("/api/agents/v2/reset")
    assert seen["keep"] is False


def test_reset_can_keep_the_global_default(client, monkeypatch):
    seen: dict = {}

    def _reset(keep_default=False):
        seen["keep"] = keep_default
        return {"ok": True}
    monkeypatch.setattr(ag._acfg, "reset", _reset)
    client.post("/api/agents/v2/reset?keep_default=true")
    assert seen["keep"] is True
