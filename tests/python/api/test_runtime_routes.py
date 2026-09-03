"""Runtime knobs: rate limits, the active backend, and the LLM settings store.

Every write here does two things — set the process env so the change is live,
and persist to runtime.env so it survives a restart — and the persistence is
best-effort: a read-only config dir must not turn a toggle into a 500.

The settings validator is the ONLY write path into the store, which is why its
bounds have to match the store's own: a mismatch makes the store's bound
unreachable and the UI 422s on a value it offered. Setting and unsetting the
same knob in one request is refused outright, because answering 200 would hide
which half won.
"""
from __future__ import annotations

import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aiforge_core.api.routes import runtime as rt


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(rt.router)
    return TestClient(app)


_TOUCHED_ENV = ("AIFORGE_PRIMARY_BACKEND", "AIFORGE_DOER_PRIMARY_BACKEND",
                "AIFORGE_FORCE_FULL_PIPELINE", "AIFORGE_COMPACT_DISABLE",
                "AIFORGE_GEMINI_RPM", "AIFORGE_GEMINI_TPM",
                "AIFORGE_LLM_MAX_WAIT_S")


@pytest.fixture(autouse=True)
def persisted(monkeypatch):
    """Capture what would be written to runtime.env.

    These endpoints set ``os.environ`` DIRECTLY (that is the point — the flag
    has to take effect in this process without a restart), so monkeypatch has
    nothing to undo and a value set here would leak into every later test in
    the session. Snapshot and restore them by hand.
    """
    written: dict = {}
    monkeypatch.setattr(rt, "_persist_env",
                        lambda key, value: written.__setitem__(key, value))
    saved = {var: os.environ.get(var) for var in _TOUCHED_ENV}
    for var in _TOUCHED_ENV:
        os.environ.pop(var, None)
    yield written
    for var, old in saved.items():
        if old is None:
            os.environ.pop(var, None)
        else:
            os.environ[var] = old


@pytest.fixture()
def providers(monkeypatch):
    """A two-provider registry with one unavailable."""
    import aiforge_core.llm as llm
    entries = [{"name": "local", "available": True},
               {"name": "gemini", "available": True},
               {"name": "anthropic", "available": False}]
    monkeypatch.setattr(llm, "list_providers", lambda: entries)
    monkeypatch.setattr(llm, "rl_state", lambda name: {"tokens": 5})

    class _Prov:
        @staticmethod
        def rate_limits():
            return {"rpm": 60, "tpm": 100000}
    monkeypatch.setattr(llm.providers, "get",
                        lambda name: _Prov() if name == "gemini" else None)
    return entries


# ─── rate limits ───────────────────────────────────────────────────────


def test_each_provider_reports_its_declared_and_effective_limits(client,
                                                                 providers):
    body = client.get("/api/runtime/rate_limits").json()
    by_name = {p["provider"]: p for p in body["providers"]}
    assert by_name["gemini"]["declared"] == {"rpm": 60, "tpm": 100000}
    assert by_name["gemini"]["effective_rpm"] == 60
    assert by_name["local"]["declared"] is None
    assert by_name["local"]["effective_rpm"] == 0
    assert by_name["anthropic"]["available"] is False
    assert body["max_wait_s"] == 120


def test_an_env_override_wins_and_is_surfaced(client, providers, monkeypatch):
    monkeypatch.setenv("AIFORGE_GEMINI_RPM", "12")
    gem = next(p for p in client.get("/api/runtime/rate_limits").json()["providers"]
               if p["provider"] == "gemini")
    assert gem["effective_rpm"] == 12.0 and gem["env_override_rpm"] == "12"


def test_the_bucket_state_is_included(client, providers):
    body = client.get("/api/runtime/rate_limits").json()
    assert body["providers"][0]["state"] == {"tokens": 5}


def test_a_limit_is_set_live_and_persisted(client, persisted):
    body = client.put("/api/runtime/rate_limits",
                      json={"provider": "Gemini", "rpm": 30, "tpm": 500000}).json()
    assert body == {"provider": "gemini", "set": {"rpm": 30, "tpm": 500000}}
    assert os.environ["AIFORGE_GEMINI_RPM"] == "30"
    assert persisted["AIFORGE_GEMINI_TPM"] == "500000"


def test_only_the_given_field_is_written(client, persisted):
    client.put("/api/runtime/rate_limits", json={"provider": "gemini", "rpm": 5})
    assert "AIFORGE_GEMINI_TPM" not in persisted


def test_a_limit_needs_a_provider(client):
    r = client.put("/api/runtime/rate_limits", json={"rpm": 5})
    assert r.status_code == 400 and "provider required" in r.json()["detail"]


# ─── the active backend ────────────────────────────────────────────────


def test_the_backend_defaults_to_local(client, providers):
    body = client.get("/api/runtime/llm_backend").json()
    assert body["backend"] == "local"
    assert body["options"] == ["local", "gemini"]
    # CHANGED 2026-09-03: the bundled gemini provider was deleted (a cloud
    # endpoint that switched itself on from AIFORGE_GOOGLE_API_KEY alone), so
    # this legacy field is now a constant False. The fixture can still inject a
    # provider NAMED gemini — `options` comes from the injected registry — but
    # the route no longer claims the bundled one is available.
    assert body["gemini_available"] is False


def test_an_unavailable_configured_backend_falls_back_to_local(client, providers,
                                                               monkeypatch):
    monkeypatch.setenv("AIFORGE_PRIMARY_BACKEND", "anthropic")
    assert client.get("/api/runtime/llm_backend").json()["backend"] == "local"


def test_the_legacy_doer_key_is_still_read(client, providers, monkeypatch):
    monkeypatch.setenv("AIFORGE_DOER_PRIMARY_BACKEND", "gemini")
    assert client.get("/api/runtime/llm_backend").json()["backend"] == "gemini"


def test_switching_backend_clears_the_legacy_key(client, providers, persisted,
                                                 monkeypatch):
    """Otherwise the doer-only key shadows the global flag."""
    monkeypatch.setenv("AIFORGE_DOER_PRIMARY_BACKEND", "local")
    body = client.put("/api/runtime/llm_backend", json={"backend": "gemini"}).json()
    assert body == {"backend": "gemini", "persisted": True}
    assert os.environ["AIFORGE_PRIMARY_BACKEND"] == "gemini"
    assert "AIFORGE_DOER_PRIMARY_BACKEND" not in os.environ
    assert persisted["AIFORGE_PRIMARY_BACKEND"] == "gemini"


def test_an_unavailable_backend_is_refused(client, providers):
    r = client.put("/api/runtime/llm_backend", json={"backend": "anthropic"})
    assert r.status_code == 400 and "must be one of" in r.json()["detail"]


def test_the_legacy_aliases_still_work(client, providers, persisted):
    assert client.get("/api/runtime/doer_backend").json()["backend"] == "local"
    assert client.put("/api/runtime/doer_backend",
                      json={"backend": "gemini"}).json()["backend"] == "gemini"


# ─── pipeline + compaction toggles ─────────────────────────────────────


def test_the_full_pipeline_toggle_round_trips(client, persisted):
    assert client.get("/api/runtime/force_full_pipeline").json() == {"enabled": False}
    body = client.put("/api/runtime/force_full_pipeline",
                      json={"enabled": True}).json()
    assert body == {"enabled": True, "persisted": True}
    assert os.environ["AIFORGE_FORCE_FULL_PIPELINE"] == "1"
    assert client.get("/api/runtime/force_full_pipeline").json() == {"enabled": True}


def test_a_read_only_config_dir_does_not_break_a_toggle(client, monkeypatch):
    monkeypatch.setattr(rt, "_persist_env",
                        lambda key, value: (_ for _ in ()).throw(OSError("read-only")))
    assert client.put("/api/runtime/force_full_pipeline",
                      json={"enabled": True}).status_code == 200


def test_compaction_is_read_through_its_single_source_of_truth(client, monkeypatch):
    """The daily pass, the boot fold and the sync-loop fold all read the same
    flag."""
    from aiforge_core.runtime import compact_window
    monkeypatch.setattr(compact_window, "disabled", lambda: True)
    assert client.get("/api/runtime/compaction").json() == {"disabled": True}


def test_compaction_can_be_disabled(client, persisted):
    body = client.put("/api/runtime/compaction", json={"disabled": True}).json()
    assert body == {"disabled": True, "persisted": True}
    assert os.environ["AIFORGE_COMPACT_DISABLE"] == "1"
    assert persisted["AIFORGE_COMPACT_DISABLE"] == "1"


def test_re_enabling_compaction_writes_zero_not_an_unset(client, persisted):
    client.put("/api/runtime/compaction", json={"disabled": False})
    assert persisted["AIFORGE_COMPACT_DISABLE"] == "0"


def test_a_failed_compaction_persist_is_not_fatal(client, monkeypatch):
    monkeypatch.setattr(rt, "_persist_env",
                        lambda key, value: (_ for _ in ()).throw(OSError("ro")))
    assert client.put("/api/runtime/compaction",
                      json={"disabled": True}).status_code == 200


# ─── per-role parameter tuning ─────────────────────────────────────────


def test_a_role_parameter_becomes_an_env_var(client):
    body = client.post("/api/runtime/session_param",
                       json={"role": "doer", "key": "temperature",
                             "value": 0.2}).json()
    assert body == {"set": "AIFORGE_DOER_TEMPERATURE", "value": "0.2"}
    assert os.environ["AIFORGE_DOER_TEMPERATURE"] == "0.2"
    os.environ.pop("AIFORGE_DOER_TEMPERATURE", None)


@pytest.mark.parametrize("payload", [
    {"key": "temperature", "value": 1},
    {"role": "doer", "value": 1},
    {"role": "doer", "key": "temperature"},
])
def test_every_field_is_required(client, payload):
    r = client.post("/api/runtime/session_param", json=payload)
    assert r.status_code == 400 and "required" in r.json()["detail"]


# ─── the empty legacy aggregates ───────────────────────────────────────


def test_token_usage_is_empty_shaped(client):
    assert client.get("/api/runtime/token_usage").json() == {"all": {},
                                                             "per_ticket": {}}


def test_metrics_keep_their_shape_for_old_callers(client):
    body = client.get("/api/metrics").json()
    assert body["ticket_grid"] == []
    assert body["feedback_verdicts"] == {"pass": 0, "fail": 0,
                                          "implicit_pass": 0}
    assert "memory_by_tier" in body


# ─── the LLM settings store ────────────────────────────────────────────


@pytest.fixture()
def settings(monkeypatch):
    from aiforge_core.config import runtime_settings as rs
    state: dict = {"all": {"llm_max_rpm": 0}, "set": None, "unset": None,
                   "error": None}
    monkeypatch.setattr(rs, "all_settings", lambda: state["all"])
    monkeypatch.setattr(rs, "_SPEC", {"llm_max_rpm": {}, "chat_rpm": {}})

    def _set_many(vals):
        if state["error"]:
            raise state["error"]
        state["set"] = vals
        return {"applied": vals}
    monkeypatch.setattr(rs, "set_many", _set_many)
    monkeypatch.setattr(rs, "unset",
                        lambda names: state.update(unset=names) or {"forgot": names})
    return state


def test_the_settings_are_read_back(client, settings):
    assert client.get("/api/runtime/llm-settings").json() == {"llm_max_rpm": 0}


def test_only_the_supplied_knobs_are_written(client, settings):
    client.put("/api/runtime/llm-settings",
               json={"llm_max_rpm": 20, "chat_rpm": 10})
    assert settings["set"] == {"llm_max_rpm": 20, "chat_rpm": 10}


def test_a_knob_can_be_forgotten_so_the_env_default_applies(client, settings):
    """The store otherwise shadows the documented env var forever."""
    body = client.put("/api/runtime/llm-settings",
                      json={"unset": ["llm_max_rpm"]}).json()
    assert settings["unset"] == ["llm_max_rpm"] and body == {"forgot":
                                                             ["llm_max_rpm"]}


def test_setting_and_unsetting_the_same_knob_is_refused(client, settings):
    """Answering 200 would hide which half won."""
    r = client.put("/api/runtime/llm-settings",
                   json={"llm_max_rpm": 5, "unset": ["llm_max_rpm"]})
    assert r.status_code == 400 and "cannot set and unset" in r.json()["detail"]


def test_forgetting_an_unknown_knob_is_refused(client, settings):
    r = client.put("/api/runtime/llm-settings", json={"unset": ["nonsense"]})
    assert r.status_code == 400 and "unknown setting" in r.json()["detail"]


def test_an_empty_request_is_refused(client, settings):
    r = client.put("/api/runtime/llm-settings", json={})
    assert r.status_code == 400 and "no settings provided" in r.json()["detail"]


def test_a_value_the_store_rejects_is_a_400(client, settings):
    settings["error"] = ValueError("llm_max_rpm out of range")
    r = client.put("/api/runtime/llm-settings", json={"llm_max_rpm": 5})
    assert r.status_code == 400 and "out of range" in r.json()["detail"]


@pytest.mark.parametrize("payload", [
    {"max_output_tokens": 1},              # below the floor
    {"context_window": 10},                # below the floor
    {"vision_capable": 2},                 # not a flag
    {"chat_unattended_cap": 0},            # a background run is never uncapped
    {"llm_rate_limit_cap_s": 0},           # must be at least a second
])
def test_out_of_range_values_are_rejected_by_the_validator(client, settings,
                                                           payload):
    assert client.put("/api/runtime/llm-settings", json=payload).status_code == 422


@pytest.mark.parametrize("payload", [
    {"chat_safety_cap": 0},                # 0 = no cap, same as the deadline
    {"chat_turn_deadline_s": 0},
    {"llm_max_rpm": 0},                    # 0 = no ceiling
    {"llm_rate_limit_backoff_s": 0},
])
def test_zero_means_no_limit_and_is_accepted(client, settings, payload):
    """A floor of 1 here made the store's own lower bound unreachable and the
    UI 422'd on a value it offers."""
    assert client.put("/api/runtime/llm-settings", json=payload).status_code == 200


# ─── perf + cost ───────────────────────────────────────────────────────


def test_the_perf_snapshot_is_served(client, monkeypatch):
    from aiforge_core.runtime import perf_recorder
    monkeypatch.setattr(perf_recorder, "aggregate", lambda: [{"step": "doer"}])
    body = client.get("/api/runtime/perf").json()
    assert body == {"rows": [{"step": "doer"}], "reset": False}


def test_the_recorder_can_be_truncated(client, monkeypatch):
    from aiforge_core.runtime import perf_recorder
    reset: list = []
    monkeypatch.setattr(perf_recorder, "reset", lambda: reset.append(1))
    monkeypatch.setattr(perf_recorder, "aggregate",
                        lambda: pytest.fail("aggregated after a reset"))
    assert client.get("/api/runtime/perf?reset=true").json() == {"rows": [],
                                                                 "reset": True}
    assert reset == [1]


def test_cost_totals_are_served(client, monkeypatch):
    from aiforge_core.observability import cost
    monkeypatch.setattr(cost, "snapshot",
                        lambda ticket: {"usd": 1.25, "ticket": ticket})
    assert client.get("/api/runtime/cost?ticket=ONE-1").json()["ticket"] == "ONE-1"


def test_a_cost_rollup_is_grouped_and_bounded(client, monkeypatch):
    from aiforge_core.observability import cost
    seen: dict = {}
    monkeypatch.setattr(cost, "rollup",
                        lambda group_by, days_back=30: seen.update(
                            group_by=group_by, days=days_back) or [{"day": "x"}])
    body = client.get("/api/runtime/cost?group_by=day&days_back=7").json()
    assert body["group_by"] == "day" and body["days_back"] == 7
    assert seen == {"group_by": "day", "days": 7}
