"""A model is called on ITS OWN endpoint, never on another model's.

Models are registered one at a time, each with its own base_url. The paths that
pass a MODEL ID around — picking the chat model, a role row saved without a URL
— used to keep whatever endpoint was already configured, so a model added from a
SECOND server was called on the FIRST server, which has never served that id.
"""
from aiforge_core.config import model_registry as mr


def _reg(monkeypatch, tmp_path, rows):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    for r in rows:
        mr.add_model(**r)


# ── the registry lookup ────────────────────────────────────────────────────

def test_connection_for_returns_the_models_own_endpoint(monkeypatch, tmp_path):
    _reg(monkeypatch, tmp_path, [
        {"label": "first", "model": "qwen-a", "base_url": "http://host-a:1234/v1",
         "api_key": "key-a", "insecure_tls": True},
        {"label": "second", "model": "llama-b", "base_url": "http://host-b:5678/v1",
         "api_key": "key-b", "insecure_tls": False},
    ])
    assert mr.connection_for("llama-b") == {
        "base_url": "http://host-b:5678/v1",
        "api_key": "key-b",
        "insecure_tls": False,
    }
    assert mr.connection_for("qwen-a")["base_url"] == "http://host-a:1234/v1"


def test_connection_for_unknown_model_is_none(monkeypatch, tmp_path):
    """None = "registry has no opinion", so the caller's own fallback stands
    (an env-pinned model was never added here)."""
    _reg(monkeypatch, tmp_path, [
        {"label": "first", "model": "qwen-a", "base_url": "http://host-a:1234/v1"}])
    assert mr.connection_for("never-registered") is None
    assert mr.connection_for("") is None


def test_connection_for_refuses_to_guess_between_two_endpoints(monkeypatch, tmp_path):
    """The SAME id served from two hosts is exactly the case that must not be
    guessed — unless the caller says which base_url it means."""
    _reg(monkeypatch, tmp_path, [
        {"label": "local", "model": "qwen-a", "base_url": "http://host-a:1234/v1"},
        {"label": "remote", "model": "qwen-a", "base_url": "http://host-b:5678/v1"},
    ])
    assert mr.connection_for("qwen-a") is None
    assert mr.connection_for("qwen-a", "http://host-b:5678/v1")["base_url"] \
        == "http://host-b:5678/v1"


def test_connection_for_row_without_a_url_is_none(monkeypatch, tmp_path):
    _reg(monkeypatch, tmp_path, [{"label": "bare", "model": "qwen-a"}])
    assert mr.connection_for("qwen-a") is None


# ── picking a chat model ───────────────────────────────────────────────────

def test_picking_a_model_moves_the_endpoint_with_it(monkeypatch, tmp_path):
    """PUT /api/chat/model must not leave the chat slot pointed at the model it
    was using before."""
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    mr.add_model(label="first", model="qwen-a", base_url="http://host-a:1234/v1")
    mr.add_model(label="second", model="llama-b", base_url="http://host-b:5678/v1",
                 api_key="key-b")

    from aiforge_core.api.routes import chat as chat_routes

    saved: dict = {}

    def _fake_set_role(role, provider, model, **kw):
        saved[role] = {"provider": provider, "model": model, **kw}
        return {"model": model, **kw}

    monkeypatch.setattr(chat_routes._acfg, "archetypes", lambda: ["chat", "_default"])
    monkeypatch.setattr(chat_routes._acfg, "get",
                        lambda role: {"provider": "openai_compatible",
                                      "model": "qwen-a",
                                      "base_url": "http://host-a:1234/v1"})
    monkeypatch.setattr(chat_routes._acfg, "set_role", _fake_set_role)
    monkeypatch.setattr(chat_routes, "_served_model_ids_for_role", lambda _r: [])
    monkeypatch.setattr(chat_routes, "_model_env_override", lambda _r: None)

    body = chat_routes._ChatModelBody(model="llama-b", apply_all=True)
    chat_routes.chat_model_set(body)

    assert saved["chat"]["base_url"] == "http://host-b:5678/v1"
    assert saved["chat"]["api_key"] == "key-b"
    assert saved["_default"]["base_url"] == "http://host-b:5678/v1"


def test_picking_an_unregistered_model_keeps_the_current_endpoint(monkeypatch, tmp_path):
    """No registry row → nothing better is known, so the slot's URL stands."""
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    from aiforge_core.api.routes import chat as chat_routes

    saved: dict = {}
    monkeypatch.setattr(chat_routes._acfg, "archetypes", lambda: ["chat"])
    monkeypatch.setattr(chat_routes._acfg, "get",
                        lambda role: {"provider": "openai_compatible",
                                      "model": "qwen-a",
                                      "base_url": "http://host-a:1234/v1"})
    monkeypatch.setattr(chat_routes._acfg, "set_role",
                        lambda role, provider, model, **kw:
                            saved.setdefault(role, kw) or {"model": model, **kw})
    monkeypatch.setattr(chat_routes, "_served_model_ids_for_role", lambda _r: [])
    monkeypatch.setattr(chat_routes, "_model_env_override", lambda _r: None)

    chat_routes.chat_model_set(
        chat_routes._ChatModelBody(model="env-pinned-model", apply_all=False))
    assert saved["chat"]["base_url"] == "http://host-a:1234/v1"


# ── role resolution ────────────────────────────────────────────────────────

def test_role_row_without_a_url_uses_its_own_models_endpoint(monkeypatch, tmp_path):
    """A role row saved with only a model id inherited the GLOBAL endpoint —
    which is the first model's host when the model belongs to another."""
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    mr.add_model(label="second", model="llama-b", base_url="http://host-b:5678/v1",
                 api_key="key-b", insecure_tls=False)
    from aiforge_core.config.agent_config import _resolve

    seed = {"provider": "openai_compatible", "model": "qwen-a",
            "base_url": "http://host-a:1234/v1", "api_key": "key-a",
            "insecure_tls": True}
    merged = _resolve._merged_row(seed, {"model": "llama-b"})
    assert merged["base_url"] == "http://host-b:5678/v1"
    assert merged["api_key"] == "key-b"
    assert merged["insecure_tls"] is False


def test_explicit_row_url_still_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    mr.add_model(label="second", model="llama-b", base_url="http://host-b:5678/v1")
    from aiforge_core.config.agent_config import _resolve

    seed = {"provider": "openai_compatible", "model": "qwen-a",
            "base_url": "http://host-a:1234/v1", "api_key": "key-a",
            "insecure_tls": True}
    merged = _resolve._merged_row(
        seed, {"model": "llama-b", "base_url": "http://pinned:9999/v1"})
    assert merged["base_url"] == "http://pinned:9999/v1"


def test_unregistered_model_still_inherits_the_seed(monkeypatch, tmp_path):
    """Back-compat: the seed fallback is only bypassed when the registry names
    an endpoint for that exact model."""
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    from aiforge_core.config.agent_config import _resolve

    seed = {"provider": "openai_compatible", "model": "qwen-a",
            "base_url": "http://host-a:1234/v1", "api_key": "key-a",
            "insecure_tls": True}
    merged = _resolve._merged_row(seed, {"model": "not-in-registry"})
    assert merged["base_url"] == "http://host-a:1234/v1"


# ── the picker can express WHICH copy ──────────────────────────────────────

def test_two_copies_of_one_model_are_two_pickable_entries(monkeypatch, tmp_path):
    """Keyed on the id alone, the second registration vanished from the picker:
    'use the copy on the other host' could not even be expressed."""
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    mr.add_model(label="local", model="qwen-a", base_url="http://host-a:1234/v1")
    mr.add_model(label="remote", model="qwen-a", base_url="http://host-b:5678/v1")
    from aiforge_core.api.routes import chat as chat_routes

    entries = chat_routes._merge_registry_and_served(
        served=["qwen-a"], current_url="http://host-a:1234/v1")
    urls = sorted(e["base_url"] for e in entries)
    assert urls == ["http://host-a:1234/v1", "http://host-b:5678/v1"]
    # "served" came from host A, so it says nothing about the copy on host B.
    by_url = {e["base_url"]: e for e in entries}
    assert by_url["http://host-a:1234/v1"]["active"] is True
    assert by_url["http://host-b:5678/v1"]["active"] is False


def test_picking_names_the_endpoint(monkeypatch, tmp_path):
    """With the endpoint on the request, the ambiguous id resolves."""
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    mr.add_model(label="local", model="qwen-a", base_url="http://host-a:1234/v1")
    mr.add_model(label="remote", model="qwen-a", base_url="http://host-b:5678/v1",
                 api_key="key-b")
    from aiforge_core.api.routes import chat as chat_routes

    saved: dict = {}
    monkeypatch.setattr(chat_routes._acfg, "archetypes", lambda: ["chat"])
    monkeypatch.setattr(chat_routes._acfg, "get",
                        lambda role: {"provider": "openai_compatible",
                                      "model": "qwen-a",
                                      "base_url": "http://host-a:1234/v1"})
    monkeypatch.setattr(chat_routes._acfg, "set_role",
                        lambda role, provider, model, **kw:
                            saved.setdefault(role, kw) or {"model": model, **kw})
    monkeypatch.setattr(chat_routes, "_served_model_ids_for_role", lambda _r: [])
    monkeypatch.setattr(chat_routes, "_model_env_override", lambda _r: None)

    chat_routes.chat_model_set(chat_routes._ChatModelBody(
        model="qwen-a", base_url="http://host-b:5678/v1", apply_all=False))
    assert saved["chat"]["base_url"] == "http://host-b:5678/v1"
    assert saved["chat"]["api_key"] == "key-b"


def test_an_explicit_endpoint_is_honoured_even_if_unregistered(monkeypatch, tmp_path):
    """The caller named the endpoint — that beats falling back to the slot's."""
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    from aiforge_core.api.routes import chat as chat_routes

    saved: dict = {}
    monkeypatch.setattr(chat_routes._acfg, "archetypes", lambda: ["chat"])
    monkeypatch.setattr(chat_routes._acfg, "get",
                        lambda role: {"provider": "openai_compatible",
                                      "model": "qwen-a",
                                      "base_url": "http://host-a:1234/v1"})
    monkeypatch.setattr(chat_routes._acfg, "set_role",
                        lambda role, provider, model, **kw:
                            saved.setdefault(role, kw) or {"model": model, **kw})
    monkeypatch.setattr(chat_routes, "_served_model_ids_for_role", lambda _r: [])
    monkeypatch.setattr(chat_routes, "_model_env_override", lambda _r: None)

    chat_routes.chat_model_set(chat_routes._ChatModelBody(
        model="unlisted", base_url="http://host-c:9999/v1", apply_all=False))
    assert saved["chat"]["base_url"] == "http://host-c:9999/v1"
