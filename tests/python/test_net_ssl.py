"""Tests for the host-scoped TLS context resolver (aiforge_core.net.ssl).

The verify opt-out (AIFORGE_LLM_SSL_VERIFY=false) must relax TLS *only*
for AIForge's own self-hosted hosts (loopback / private IP / .local /
configured base_url hosts) and NEVER for public hosts. A CA bundle keeps
verification on for every host.
"""
from __future__ import annotations

import ssl

import pytest

from aiforge_core.llm import _ssl as ssl_shim
from aiforge_core.net.ssl import context_for


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in (
        "AIFORGE_LLM_SSL_VERIFY", "AIFORGE_LLM_CA_BUNDLE",
        "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE",
        "AIFORGE_LM_BASE_URL", "AIFORGE_EMBED_URL", "AIFORGE_RERANK_URL",
        "AIFORGE_MCP_ENDPOINTS", "AIFORGE_API_BASE",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


def test_http_returns_none():
    assert context_for("http://127.0.0.1:1234/v1") is None
    assert context_for(None) is None
    assert context_for("") is None


def test_shim_reexports_same_callable():
    assert ssl_shim.context_for is context_for


def test_default_verifies_internal_https():
    # No env -> secure by default, even for an internal host.
    ctx = context_for("https://127.0.0.1:1234/v1")
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


@pytest.mark.parametrize("url", [
    "https://127.0.0.1:1234/v1/models",
    "https://localhost:8764/embed",
    "https://10.0.0.5:8765/rerank",
    "https://192.168.70.115:8810/mcp",
    "https://172.16.3.4/v1",
    "https://my-llm.local/v1",
    "https://mybox/v1",  # bare hostname = LAN by convention
])
def test_verify_off_relaxes_internal_hosts(monkeypatch, url):
    monkeypatch.setenv("AIFORGE_LLM_SSL_VERIFY", "false")
    ctx = context_for(url)
    assert ctx.verify_mode == ssl.CERT_NONE
    assert ctx.check_hostname is False


@pytest.mark.parametrize("url", [
    "https://api.github.com/repos/x/y",
    "https://example.com/doc.html",
    "https://8.8.8.8/x",  # public IP
])
def test_verify_off_keeps_public_hosts_strict(monkeypatch, url):
    monkeypatch.setenv("AIFORGE_LLM_SSL_VERIFY", "false")
    ctx = context_for(url)
    assert ctx.verify_mode == ssl.CERT_REQUIRED


def test_configured_base_url_host_is_trusted(monkeypatch):
    # A public-looking DNS name that the operator points the model at
    # counts as a host they control -> relaxed when verify is off.
    monkeypatch.setenv("AIFORGE_LLM_SSL_VERIFY", "false")
    monkeypatch.setenv("AIFORGE_LM_BASE_URL", "https://llm.mycorp.example/v1")
    ctx = context_for("https://llm.mycorp.example/v1/chat/completions")
    assert ctx.verify_mode == ssl.CERT_NONE
    # A different, non-configured public host is still strict.
    other = context_for("https://other.example/x")
    assert other.verify_mode == ssl.CERT_REQUIRED


def test_mcp_endpoint_host_is_trusted(monkeypatch):
    monkeypatch.setenv("AIFORGE_LLM_SSL_VERIFY", "false")
    monkeypatch.setenv(
        "AIFORGE_MCP_ENDPOINTS",
        "mongo=https://mcp.lab.example:8810,k8s=https://mcp.lab.example:8811",
    )
    ctx = context_for("https://mcp.lab.example:8810/mcp")
    assert ctx.verify_mode == ssl.CERT_NONE


def test_ca_bundle_keeps_verification_on_even_for_public(monkeypatch, tmp_path):
    # A real PEM so create_default_context(cafile=...) succeeds.
    import subprocess
    crt = tmp_path / "ca.pem"
    key = tmp_path / "ca.key"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-keyout", str(key),
         "-out", str(crt), "-days", "1", "-nodes", "-subj", "/CN=test-ca"],
        check=True, capture_output=True,
    )
    monkeypatch.setenv("AIFORGE_LLM_SSL_VERIFY", "false")  # ignored when CA set
    monkeypatch.setenv("AIFORGE_LLM_CA_BUNDLE", str(crt))
    for url in ("https://127.0.0.1:1234/v1", "https://api.github.com/x"):
        ctx = context_for(url)
        assert ctx.verify_mode == ssl.CERT_REQUIRED


@pytest.mark.parametrize("val", ["0", "false", "no", "off", ""])
def test_falsey_values_disable_for_internal(monkeypatch, val):
    monkeypatch.setenv("AIFORGE_LLM_SSL_VERIFY", val)
    assert context_for("https://127.0.0.1/v1").verify_mode == ssl.CERT_NONE


@pytest.mark.parametrize("val", ["1", "true", "yes", "on", "TRUE"])
def test_truthy_values_keep_verification(monkeypatch, val):
    monkeypatch.setenv("AIFORGE_LLM_SSL_VERIFY", val)
    assert context_for("https://127.0.0.1/v1").verify_mode == ssl.CERT_REQUIRED
