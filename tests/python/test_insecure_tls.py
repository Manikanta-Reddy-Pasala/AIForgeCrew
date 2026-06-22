"""Per-endpoint TLS opt-out (`insecure_tls`) — the UI checkbox path.

Covers the full chain that lets an operator point AIForge at a
self-signed / internal HTTPS model box (e.g. ``https://chatai.internal``)
without editing env files + restarting:

  set_role(insecure_tls=True) → stored on the row
  resolve_litellm()           → surfaces insecure_tls
  escalating_llm._build_one() → passes ssl_verify=False to LiteLLM
  probe(insecure=True)        → builds a CERT_NONE context
  net.ssl.insecure_context()  → CERT_NONE / no hostname check
  _ensure_v1()                → respects an operator-supplied path (/api)
"""
import importlib
import ssl

import pytest


@pytest.fixture
def cfgdir(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    import os
    for k in list(os.environ):
        if k.startswith("AIFORGE_") and (
            k.endswith("_BASE_URL") or k.endswith("_API_KEY")
            or k.endswith("_MODEL") or k.endswith("_PROVIDER")
        ):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("AIFORGE_LLM_SSL_VERIFY", raising=False)
    monkeypatch.delenv("AIFORGE_LLM_CA_BUNDLE", raising=False)
    import aiforge_core.config.agent_config as acfg
    importlib.reload(acfg)
    return acfg


# ── net.ssl.insecure_context ─────────────────────────────────────────
def test_insecure_context_is_cert_none():
    from aiforge_core.net.ssl import insecure_context
    ctx = insecure_context()
    assert ctx.verify_mode == ssl.CERT_NONE
    assert ctx.check_hostname is False


def test_shim_reexports_insecure_context():
    from aiforge_core.llm._ssl import insecure_context as b
    from aiforge_core.net.ssl import insecure_context as a
    assert a is b


# ── _ensure_v1 respects operator-supplied paths ──────────────────────
@pytest.mark.parametrize("given,expect", [
    ("http://box:1234", "http://box:1234/v1"),        # bare host → +/v1
    ("http://box:1234/", "http://box:1234/v1"),       # trailing slash
    ("http://box:1234/v1", "http://box:1234/v1"),     # already /v1
    ("https://chatai.internal/api", "https://chatai.internal/api"),  # OWUI
    ("https://host/openai/v1", "https://host/openai/v1"),
])
def test_ensure_v1_path_aware(given, expect):
    from aiforge_core.llm.providers.openai_compatible import _ensure_v1
    assert _ensure_v1(given) == expect


# ── set_role persists insecure_tls ───────────────────────────────────
def test_set_role_persists_insecure_tls(cfgdir):
    row = cfgdir.set_role("doer", "openai_compatible", "m",
                          base_url="https://chatai.internal", insecure_tls=True)
    assert row["insecure_tls"] is True
    # default stays False when omitted
    row2 = cfgdir.set_role("planner", "openai_compatible", "m",
                           base_url="https://x.internal")
    assert row2["insecure_tls"] is False


def test_resolve_litellm_surfaces_insecure_tls(cfgdir):
    cfgdir.set_role("doer", "openai_compatible", "m",
                    base_url="https://chatai.internal", insecure_tls=True)
    out = cfgdir.resolve_litellm("doer")
    assert out["insecure_tls"] is True


# ── escalating_llm._build_one wires ssl_verify=False ─────────────────
def test_build_one_passes_ssl_verify_false_on_insecure(monkeypatch):
    captured = {}

    class _FakeLiteLlm:
        def __init__(self, **kw):
            captured.update(kw)

    import google.adk.models.lite_llm as ll
    monkeypatch.setattr(ll, "LiteLlm", _FakeLiteLlm)
    from aiforge_core.runtime import escalating_llm
    escalating_llm._build_one({
        "model_id": "openai/m", "api_base": "https://chatai.internal/v1",
        "api_key": "k", "insecure_tls": True,
    })
    assert captured.get("ssl_verify") is False


def test_build_one_no_ssl_verify_when_secure(monkeypatch):
    captured = {}

    class _FakeLiteLlm:
        def __init__(self, **kw):
            captured.update(kw)

    import google.adk.models.lite_llm as ll
    monkeypatch.setattr(ll, "LiteLlm", _FakeLiteLlm)
    monkeypatch.delenv("AIFORGE_LLM_SSL_VERIFY", raising=False)
    monkeypatch.delenv("AIFORGE_LLM_CA_BUNDLE", raising=False)
    from aiforge_core.runtime import escalating_llm
    escalating_llm._build_one({
        "model_id": "openai/m", "api_base": "https://chatai.internal/v1",
        "api_key": "k", "insecure_tls": False,
    })
    assert "ssl_verify" not in captured


# ── probe(insecure=True) selects the CERT_NONE context ───────────────
def test_probe_insecure_uses_unverified_context(monkeypatch):
    import aiforge_core.llm.providers.openai_compatible as oc

    seen = {}

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"data":[{"id":"m"}]}'

    def _fake_urlopen(req, timeout=None, context=None):
        seen["context"] = context
        return _Resp()

    monkeypatch.setattr(oc.urllib.request, "urlopen", _fake_urlopen)
    out = oc.probe("https://chatai.internal", insecure=True)
    assert out["ok"] is True
    assert out["models"] == ["m"]
    assert seen["context"].verify_mode == ssl.CERT_NONE
