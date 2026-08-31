"""The model registry: connections, capability routing, and the context window.

Two mistakes this file exists to prevent. Selecting a model added from a SECOND
server used to keep the FIRST server's URL, so every call went to a host that
had never heard of it — hence each row carries its OWN connection, and an
ambiguous id (registered twice, no url to disambiguate) resolves to nothing
rather than a guess.

And capability routing: a reasoning model spends its whole budget thinking and
returns EMPTY on short direct tasks, which is what made simple chat answers
come back blank. So fast roles get the non-thinking coder, and the blanket
default is the coder too — not the largest-context model.
"""
from __future__ import annotations

import json

import pytest

from aiforge_core.config import model_registry as mr


@pytest.fixture(autouse=True)
def registry(monkeypatch, tmp_path):
    path = tmp_path / "model_registry.json"
    monkeypatch.setattr(mr, "_path", lambda: str(path))
    monkeypatch.delenv("AIFORGE_AUTODETECT_CTX", raising=False)
    return path


def _add(**kw):
    base = {"label": "Coder", "model": "qwen3-coder", "base_url": "http://boxA/v1"}
    base.update(kw)
    return mr.add_model(**base)


# ─── capability heuristics ─────────────────────────────────────────────


@pytest.mark.parametrize("model_id", ["qwen3-30b-thinking", "deepseek-r1",
                                      "o3-mini", "qwq-32b"])
def test_reasoning_models_are_recognised(model_id):
    assert mr.detect_capability(model_id, "thinking") is True


@pytest.mark.parametrize("model_id", ["qwen2.5-vl-7b", "llava-1.6", "pixtral-12b",
                                      "gemma-3-27b", "internvl2"])
def test_vision_models_are_recognised(model_id):
    assert mr.detect_capability(model_id, "vision") is True


@pytest.mark.parametrize("model_id", ["qwen3-coder", "llama-3.1-8b", ""])
def test_a_plain_model_has_neither_capability(model_id):
    assert mr.detect_capability(model_id, "thinking") is False
    assert mr.detect_capability(model_id, "vision") is False


@pytest.mark.parametrize("flag,expected", [("yes", True), ("no", False),
                                           ("auto", True), ("", True)])
def test_an_explicit_flag_overrides_the_heuristic(flag, expected):
    assert mr._resolve(flag, "qwen2.5-vl-7b", "vision") is expected


# ─── storage ───────────────────────────────────────────────────────────


def test_a_model_is_added_and_listed(registry):
    row = _add()
    assert row["id"] == "coder" and row["model"] == "qwen3-coder"
    assert row["api_key_set"] is False
    listed = mr.list_models()
    assert len(listed) == 1 and listed[0]["base_url"] == "http://boxA/v1"


def test_a_model_id_is_required():
    with pytest.raises(ValueError, match="model id is required"):
        mr.add_model(label="x", model="  ")


def test_duplicate_labels_get_distinct_ids(registry):
    assert [_add()["id"], _add()["id"], _add()["id"]] == ["coder", "coder-2",
                                                          "coder-3"]


def test_an_invalid_capability_flag_falls_back_to_auto(registry):
    row = _add(vision="maybe", thinking="perhaps")
    assert row["vision"] == "auto" and row["thinking"] == "auto"


def test_the_raw_key_is_never_listed(registry):
    _add(api_key="sk-secret")
    assert "sk-secret" not in json.dumps(mr.list_models())
    assert mr.list_models()[0]["api_key_set"] is True


def test_a_corrupt_registry_reads_as_empty(registry):
    registry.write_text("{not a list}")
    assert mr.list_models() == []


def test_a_model_is_updated_field_by_field(registry):
    mid = _add()["id"]
    out = mr.update_model(mid, label="New", base_url="http://boxB/v1",
                          vision="yes", thinking="no", context_window=32768,
                          insecure_tls=False)
    assert out["label"] == "New" and out["base_url"] == "http://boxB/v1"
    assert out["has_vision"] is True and out["has_thinking"] is False
    assert out["context_window"] == 32768 and out["insecure_tls"] is False


def test_an_empty_key_never_clears_the_stored_one(registry):
    mid = _add(api_key="sk-secret")["id"]
    mr.update_model(mid, api_key="")
    assert mr.get_model(mid)["api_key"] == "sk-secret"


def test_a_negative_context_window_is_floored(registry):
    mid = _add()["id"]
    assert mr.update_model(mid, context_window=-5)["context_window"] == 0


def test_updating_a_missing_model(registry):
    assert mr.update_model("nope", label="x") is None


def test_a_model_is_removed(registry):
    mid = _add()["id"]
    assert mr.remove_model(mid) is True and mr.list_models() == []
    assert mr.remove_model(mid) is False


# ─── which connection a model means ────────────────────────────────────


def test_a_model_resolves_to_its_own_endpoint(registry):
    """Selecting a model added from a second server used to keep the first
    server's URL, and every call went to a host that never served it."""
    _add(label="A", model="coder", base_url="http://boxA/v1", api_key="sk-a")
    _add(label="B", model="thinker", base_url="http://boxB/v1", api_key="sk-b",
         insecure_tls=False)
    assert mr.connection_for("thinker") == {"base_url": "http://boxB/v1",
                                            "api_key": "sk-b",
                                            "insecure_tls": False}


def test_an_ambiguous_id_is_never_guessed(registry):
    """The same id served from two endpoints is exactly the case that must not
    be guessed between."""
    _add(label="A", model="coder", base_url="http://boxA/v1")
    _add(label="B", model="coder", base_url="http://boxB/v1")
    assert mr.connection_for("coder") is None
    assert mr.connection_for("coder", "http://boxB/v1")["base_url"] == \
        "http://boxB/v1"


def test_an_unregistered_model_leaves_the_caller_its_own_fallback(registry):
    assert mr.connection_for("never-added") is None
    assert mr.connection_for("") is None


def test_a_row_without_a_url_has_no_endpoint_to_prefer(registry):
    _add(base_url="")
    assert mr.connection_for("qwen3-coder") is None


# ─── the fallback chain ────────────────────────────────────────────────


def test_the_other_models_are_offered_as_fallbacks(registry):
    """A dead model was the end of the road on a single-provider install."""
    _add(label="A", model="coder", base_url="http://boxA/v1")
    _add(label="B", model="thinker", base_url="http://boxA/v1")
    chain = mr.chain_after("coder", "http://boxA/v1")
    assert [r["model"] for r in chain] == ["thinker"]


def test_the_same_model_on_another_box_is_a_valid_fallback(registry):
    """That is the textbook redundancy setup — exclusion is by CONNECTION."""
    _add(label="A", model="coder", base_url="http://boxA/v1")
    _add(label="B", model="coder", base_url="http://boxB/v1")
    chain = mr.chain_after("coder", "http://boxA/v1")
    assert [r["base_url"] for r in chain] == ["http://boxB/v1"]


def test_embedding_models_never_burn_a_fallback_round(registry):
    _add(label="A", model="coder", base_url="http://boxA/v1")
    _add(label="E", model="nomic-embed-text", base_url="http://boxA/v1")
    assert mr.chain_after("coder", "http://boxA/v1") == []


def test_a_urlless_row_counts_as_the_failed_endpoint(registry):
    _add(label="A", model="coder", base_url="")
    assert mr.chain_after("coder", "http://boxA/v1") == []


def test_a_hand_edited_registry_row_is_skipped(registry, monkeypatch):
    monkeypatch.setattr(mr, "_load", lambda: ["junk", {"no_model": 1},
                                              {"model": "thinker"}])
    assert [r["model"] for r in mr.chain_after("coder")] == ["thinker"]


# ─── the vision flag ───────────────────────────────────────────────────


def test_a_probed_vision_result_is_made_durable(registry):
    _add(model="mystery-model")
    assert mr.set_vision_flag("mystery-model", "http://boxA/v1", "yes") is True
    assert mr.vision_for("mystery-model") == "yes"


def test_re_flagging_the_same_value_is_a_no_op(registry):
    _add(model="m", vision="yes")
    assert mr.set_vision_flag("m", "", "yes") is True


@pytest.mark.parametrize("flag,model", [("maybe", "m"), ("yes", "")])
def test_an_invalid_flag_or_model_is_refused(registry, flag, model):
    _add(model="m")
    assert mr.set_vision_flag(model, "", flag) is False


def test_an_env_pinned_model_that_is_not_a_row_keeps_only_its_cache(registry):
    assert mr.set_vision_flag("not-registered", "", "yes") is False


def test_an_auto_flag_reads_as_unset_so_the_caller_probes(registry):
    _add(model="m", vision="auto")
    assert mr.vision_for("m") is None
    assert mr.vision_for("never-added") is None


# ─── the context window ────────────────────────────────────────────────


def test_a_per_model_window_is_read_back(registry):
    _add(model="m", context_window=32768)
    assert mr.context_for("m", "http://boxA/v1") == 32768
    assert mr.context_for("never-added") == 0


@pytest.fixture()
def window(monkeypatch):
    """Control every input to the resolution order."""
    from aiforge_core.config import runtime_settings
    from aiforge_core.llm import health, router
    state = {"per": 0, "explicit": None, "detected": None,
             "ep": type("EP", (), {"model": "m", "base_url": "http://boxA/v1",
                                   "api_key": "sk"})()}
    monkeypatch.setattr(router, "resolve", lambda role: state["ep"])
    monkeypatch.setattr(mr, "context_for", lambda model, url: state["per"])
    monkeypatch.setattr(runtime_settings, "explicit",
                        lambda key: state["explicit"])
    monkeypatch.setattr(health, "probe_context_window",
                        lambda base_url, api_key=None: state["detected"])
    return state


def test_a_per_model_window_beats_everything(window):
    window.update(per=8192, explicit=131072, detected=200000)
    assert mr.effective_context_window("doer") == 8192


def test_an_explicit_global_beats_autodetection(window):
    window.update(explicit=65536, detected=200000)
    assert mr.effective_context_window("doer") == 65536


def test_autodetection_beats_the_static_default(window):
    window["detected"] = 200000
    assert mr.effective_context_window("doer") == 200000


def test_a_detected_window_is_capped(window):
    window["detected"] = 10_000_000
    assert mr.effective_context_window("doer") == mr._CTX_CEILING


def test_the_conservative_static_default_is_the_floor(window):
    """Assuming LESS than the served window only condenses earlier; assuming
    more is the 400."""
    assert mr.effective_context_window("doer") == mr._CTX_STATIC_DEFAULT


def test_autodetection_can_be_turned_off(window, monkeypatch):
    monkeypatch.setenv("AIFORGE_AUTODETECT_CTX", "0")
    window["detected"] = 200000
    assert mr.effective_context_window("doer") == mr._CTX_STATIC_DEFAULT


def test_a_failed_probe_falls_through(window, monkeypatch):
    from aiforge_core.llm import health
    monkeypatch.setattr(health, "probe_context_window",
                        lambda base_url, api_key=None: (_ for _ in ()).throw(
                            OSError("refused")))
    assert mr.effective_context_window("doer") == mr._CTX_STATIC_DEFAULT


def test_an_unroutable_role_still_resolves(window, monkeypatch):
    from aiforge_core.llm import router
    monkeypatch.setattr(router, "resolve",
                        lambda role: (_ for _ in ()).throw(RuntimeError("no cfg")))
    assert mr.effective_context_window("doer") == mr._CTX_STATIC_DEFAULT


def test_a_broken_settings_store_falls_through(window, monkeypatch):
    from aiforge_core.config import runtime_settings
    monkeypatch.setattr(runtime_settings, "explicit",
                        lambda key: (_ for _ in ()).throw(RuntimeError("no db")))
    assert mr.effective_context_window("doer") == mr._CTX_STATIC_DEFAULT


def test_the_role_wrapper_is_the_same_resolution(window):
    window["per"] = 4096
    assert mr.context_window_for_role("doer") == 4096


# ─── seeding from the agents' config ───────────────────────────────────


def test_the_registry_is_seeded_from_the_wired_roles(registry, monkeypatch):
    from aiforge_core.config import agent_config
    monkeypatch.setattr(agent_config, "load_all", lambda: {
        "doer": {"model": "coder", "base_url": "http://boxA/v1"},
        "planner": {"model": "coder", "base_url": "http://boxA/v1"},   # dup
        "chat": {"model": "thinker", "base_url": "http://boxB/v1"},
        "blank": {"model": ""},
        "unset": {"model": "local-model-unconfigured-x"},
    })
    out = mr.sync_from_config()
    assert out["count"] == 2
    assert {m["model"] for m in mr.list_models()} == {"coder", "thinker"}


def test_a_broken_config_seeds_nothing(registry, monkeypatch):
    from aiforge_core.config import agent_config
    monkeypatch.setattr(agent_config, "load_all",
                        lambda: (_ for _ in ()).throw(RuntimeError("bad yaml")))
    assert mr.sync_from_config() == {"added": [], "count": 0}


# ─── applying a model to roles ─────────────────────────────────────────


def test_a_model_is_written_into_each_roles_config(registry, monkeypatch):
    from aiforge_core.config import agent_config
    mid = _add(api_key="sk-a")["id"]
    saved: list = []
    monkeypatch.setattr(agent_config, "set_role",
                        lambda role, provider, model, **kw: saved.append(
                            (role, model, kw.get("base_url"))))
    out = mr.apply_to_roles(mid, ["doer", "planner"])
    assert out == {"applied": ["doer", "planner"], "errors": {}}
    assert saved[0] == ("doer", "qwen3-coder", "http://boxA/v1")


def test_a_role_that_rejects_the_write_is_reported(registry, monkeypatch):
    from aiforge_core.config import agent_config
    mid = _add()["id"]

    def _set(role, provider, model, **kw):
        if role == "bad":
            raise ValueError("unknown role")
    monkeypatch.setattr(agent_config, "set_role", _set)
    out = mr.apply_to_roles(mid, ["doer", "bad"])
    assert out["applied"] == ["doer"] and "unknown role" in out["errors"]["bad"]


def test_applying_an_unknown_model(registry):
    with pytest.raises(ValueError, match="unknown model"):
        mr.apply_to_roles("nope", ["doer"])


# ─── capability-based assignment ───────────────────────────────────────


@pytest.fixture()
def catalogue(registry):
    _add(label="Coder", model="qwen3-coder", context_window=32768)
    _add(label="Thinker", model="deepseek-r1", context_window=131072)
    _add(label="BigCoder", model="qwen3-coder-480b", context_window=262144)
    _add(label="Vision", model="qwen2.5-vl-7b", context_window=16384)
    _add(label="Embed", model="nomic-embed-text")


@pytest.mark.parametrize("role", ["enhancer", "learner", "triage", "feedback",
                                  "refiner", "title", "summariser", "classifier",
                                  "ctx_memory", "live_verifier"])
def test_quick_direct_roles_are_recognised(role):
    """A reasoning model spends its budget thinking and returns EMPTY here."""
    assert mr.is_fast_role(role) is True


@pytest.mark.parametrize("role", ["planner", "doer", "architect", "chat"])
def test_other_roles_are_not_fast_roles(role):
    assert mr.is_fast_role(role) is False


def test_thinking_roles_get_the_reasoning_model(catalogue):
    plan = mr.suggest_assignments(["planner", "validator", "judge"])
    assert set(plan.values()) == {"thinker"}


def test_fast_roles_get_the_non_thinking_coder(catalogue):
    plan = mr.suggest_assignments(["enhancer", "learner"])
    assert set(plan.values()) == {"bigcoder"}          # largest-context coder


def test_coder_roles_get_the_coder(catalogue):
    assert mr.suggest_assignments(["doer"])["doer"] == "bigcoder"


def test_a_vision_role_gets_the_vision_model(catalogue):
    assert mr.suggest_assignments(["vision"])["vision"] == "vision"


def test_the_blanket_default_is_the_coder_not_the_biggest_model(catalogue):
    """A reasoning model as the default is what made simple chat come back
    empty."""
    assert mr.suggest_assignments(["chat"])["chat"] == "bigcoder"


def test_embedding_models_are_never_assigned(catalogue):
    assert "embed" not in set(mr.suggest_assignments(["chat", "doer"]).values())


def test_an_empty_registry_assigns_nothing(registry):
    assert mr.suggest_assignments(["doer"]) == {}


def test_with_only_a_reasoning_model_it_is_used_everywhere(registry):
    _add(label="Thinker", model="deepseek-r1")
    assert mr.suggest_assignments(["enhancer", "doer"]) == {"enhancer": "thinker",
                                                            "doer": "thinker"}


def test_auto_assign_applies_the_plan(catalogue, monkeypatch):
    applied: list = []
    monkeypatch.setattr(mr, "apply_to_roles",
                        lambda mid, roles: applied.append((mid, roles))
                        or {"applied": roles, "errors": {}})
    out = mr.auto_assign(["planner", "doer"])
    assert out["assignments"] == {"planner": "thinker", "doer": "bigcoder"}
    assert sorted(applied) == [("bigcoder", ["doer"]), ("thinker", ["planner"])]
