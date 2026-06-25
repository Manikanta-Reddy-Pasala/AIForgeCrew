"""Public SaaS model endpoints must verify TLS by default; only intrinsically
internal hosts auto-relax. (Regression: configuring OpenRouter silently
disabled TLS verification because its host was a 'configured service host'.)"""
from aiforge_core.net import ssl as s


def test_public_saas_endpoints_verify(monkeypatch):
    monkeypatch.delenv("AIFORGE_LLM_CA_BUNDLE", raising=False)
    for url in ("https://openrouter.ai/api/v1", "https://api.openai.com/v1",
                "https://ollama.com/v1", "https://api.anthropic.com"):
        assert s.auto_relax_internal(url) is False, url


def test_internal_hosts_still_relax():
    for url in ("https://chatai.internal", "https://10.10.10.2:1234",
                "https://lmstudio:1234", "https://127.0.0.1:8080",
                "https://box.local"):
        assert s.auto_relax_internal(url) is True, url


def test_configured_public_host_not_auto_relaxed(monkeypatch):
    # Even when OpenRouter is the configured default, the DEFAULT-ON auto-relax
    # path must keep verifying it (no explicit opt-out given).
    monkeypatch.setenv("AIFORGE_DEFAULT_PROVIDER", "openai_compatible")
    monkeypatch.setenv("AIFORGE_DEFAULT_BASE_URL", "https://openrouter.ai/api/v1")
    assert s.auto_relax_internal("https://openrouter.ai/api/v1") is False
    # The explicit verify-off path may still trust a configured host.
    monkeypatch.setenv("AIFORGE_LLM_SSL_VERIFY", "false")
    import ssl as _ssl
    ctx = s.context_for("https://openrouter.ai/api/v1")
    assert ctx.verify_mode == _ssl.CERT_NONE
