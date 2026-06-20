import importlib
import json

import pytest


@pytest.fixture
def cfgdir(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    for k in list(__import__("os").environ):
        if k.startswith("AIFORGE_") and (k.endswith("_BASE_URL")
                                         or k.endswith("_API_KEY")
                                         or k.endswith("_MODEL")
                                         or k.endswith("_PROVIDER")):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("AIFORGE_OPENAI_COMPAT_BASE_URL", raising=False)
    monkeypatch.delenv("AIFORGE_OPENAI_COMPAT_API_KEY", raising=False)
    import aiforge_core.config.agent_config as acfg
    importlib.reload(acfg)
    return acfg


def test_provider_registered():
    from aiforge_core.llm import providers as p
    assert p.get("openai_compatible") is not None


def test_in_config_providers(cfgdir):
    assert "openai_compatible" in cfgdir.PROVIDERS
    assert cfgdir.PROVIDERS["openai_compatible"]["litellm_prefix"] == "openai"


def test_set_role_accepts_openai_compatible_with_key(cfgdir):
    row = cfgdir.set_role("doer", "openai_compatible", "qwen-coder",
                          base_url="http://box:1234", api_key="sk-abc")
    assert row["provider"] == "openai_compatible"
    assert row["base_url"] == "http://box:1234"
    assert row["api_key"] == "sk-abc"


def test_endpoint_reads_config(cfgdir):
    cfgdir.set_role("doer", "openai_compatible", "my-model",
                    base_url="http://box:9999", api_key="sk-xyz")
    from aiforge_core.llm.providers.openai_compatible import OpenAICompatibleProvider
    ep = OpenAICompatibleProvider().endpoint("doer")
    assert ep.base_url == "http://box:9999/v1"   # /v1 appended
    assert ep.api_key == "sk-xyz"
    assert ep.model == "my-model"
    assert ep.provider == "openai_compatible"


def test_endpoint_blank_key_uses_no_token(cfgdir):
    cfgdir.set_role("doer", "openai_compatible", "m", base_url="http://oss:1234")
    from aiforge_core.llm.providers.openai_compatible import OpenAICompatibleProvider
    ep = OpenAICompatibleProvider().endpoint("doer")
    assert ep.api_key == "not-needed"


def test_env_overrides_config(cfgdir, monkeypatch):
    cfgdir.set_role("doer", "openai_compatible", "m", base_url="http://oss:1234")
    monkeypatch.setenv("AIFORGE_DOER_BASE_URL", "http://override:5555")
    from aiforge_core.llm.providers.openai_compatible import OpenAICompatibleProvider
    ep = OpenAICompatibleProvider().endpoint("doer")
    assert ep.base_url == "http://override:5555/v1"


def test_resolve_litellm_openai_compatible(cfgdir):
    cfgdir.set_role("doer", "openai_compatible", "my-model",
                    base_url="http://box:1234", api_key="sk-key")
    out = cfgdir.resolve_litellm("doer")
    assert out["model_id"] == "openai/my-model"
    assert out["api_base"] == "http://box:1234"
    assert out["api_key"] == "sk-key"


def test_resolve_litellm_blank_key_sentinel(cfgdir):
    cfgdir.set_role("doer", "openai_compatible", "m", base_url="http://oss:1234")
    out = cfgdir.resolve_litellm("doer")
    assert out["api_key"] == "not-needed"


def test_probe_success(monkeypatch):
    from aiforge_core.llm.providers import openai_compatible as oc

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self):
            return json.dumps({"data": [{"id": "model-a"}, {"id": "model-b"}]}).encode()

    monkeypatch.setattr(oc.urllib.request, "urlopen", lambda *a, **k: _Resp())
    out = oc.probe("http://box:1234", api_key="sk-1")
    assert out["ok"] is True
    assert out["models"] == ["model-a", "model-b"]


def test_probe_failure(monkeypatch):
    from aiforge_core.llm.providers import openai_compatible as oc

    def _boom(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(oc.urllib.request, "urlopen", _boom)
    out = oc.probe("http://box:1234")
    assert out["ok"] is False
    assert "refused" in out["error"]


def test_probe_requires_base_url():
    from aiforge_core.llm.providers import openai_compatible as oc
    assert oc.probe("")["ok"] is False
