"""Regression: the chat/client router must read the v2 agent_config.

Bug: aiforge_core.llm.router imported a non-existent
``aiforge_core.runtime.agent_config``, so the import silently failed and
every role fell back to ``local`` in the client.complete() path — the UI's
chat/provider selection never took effect, so chat ran on the (dead) local
endpoint regardless of what was saved.
"""
import importlib

import pytest


@pytest.fixture
def cfg(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    import os
    for k in list(os.environ):
        if k.startswith("AIFORGE_") and (
            k.endswith("_PROVIDER") or k.endswith("_BASE_URL")
            or k.endswith("_API_KEY") or k.endswith("_MODEL")
        ):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("AIFORGE_PRIMARY_BACKEND", raising=False)
    monkeypatch.delenv("AIFORGE_LLM_CA_BUNDLE", raising=False)
    import aiforge_core.config.agent_config as acfg
    importlib.reload(acfg)
    import aiforge_core.llm.router as router
    importlib.reload(router)
    return acfg, router


def test_router_resolves_chat_from_v2_config(cfg):
    acfg, router = cfg
    acfg.set_role("chat", "openai_compatible", "qwen3.6-35b-A3b",
                  base_url="https://chatai.internal/api",
                  api_key="sk-tok", insecure_tls=True)
    ep = router.resolve("chat")
    assert ep.provider == "openai_compatible"        # NOT local
    assert ep.base_url == "https://chatai.internal/api"
    assert ep.model == "qwen3.6-35b-A3b"
    assert ep.api_key == "sk-tok"
    assert (ep.extras or {}).get("insecure_tls") is True


def test_router_still_defaults_local_when_unset(cfg):
    _, router = cfg
    ep = router.resolve("doer")
    assert ep.provider == "local"


def test_build_body_excludes_transport_control_extras():
    # insecure_tls / claude routing keys must NOT leak into the chat body —
    # strict servers (Open WebUI) 400 on unknown completion params.
    import json

    from aiforge_core.llm.client import _build_body
    from aiforge_core.llm.types import Endpoint
    ep = Endpoint(base_url="https://chatai.internal/api", api_key="k",
                  model="qwen35-122b-reasoning", provider="openai_compatible",
                  role="chat",
                  extras={"insecure_tls": True, "claude_host": "x",
                          "chat_template_kwargs": {"enable_thinking": False}})
    body = json.loads(_build_body(ep, [{"role": "user", "content": "hi"}],
                                  None, None, None, None))
    assert "insecure_tls" not in body
    assert "claude_host" not in body
    # legitimate body extras still pass through
    assert body.get("chat_template_kwargs") == {"enable_thinking": False}
