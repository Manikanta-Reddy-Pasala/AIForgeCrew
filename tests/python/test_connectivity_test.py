"""Tests for the ./run.sh --test connectivity probe CLI."""
from __future__ import annotations

import pytest

from aiforge_core.cli import connectivity_test as ct


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in (
        "AIFORGE_LLM_SSL_VERIFY", "AIFORGE_LLM_CA_BUNDLE",
        "AIFORGE_LM_BASE_URL", "AIFORGE_DOER_BASE_URL",
        "AIFORGE_OPENAI_COMPAT_BASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


def test_env_fallback_resolution(monkeypatch):
    # When agent_config is unavailable, fall back to documented env order.
    import aiforge_core.config.agent_config as acfg
    monkeypatch.setattr(acfg, "resolve_litellm",
                        lambda role: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setenv("AIFORGE_LM_BASE_URL", "https://box:1234/v1")
    base, _ = ct._resolve_endpoint("doer")
    assert base == "https://box:1234/v1"


def test_fail_path_returns_nonzero(monkeypatch, capsys):
    # Point at a closed port -> probe fails -> exit code 1, no key leak.
    monkeypatch.setenv("AIFORGE_LM_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("AIFORGE_LM_API_KEY", "super-secret-key")
    rc = ct.main(["doer"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL" in out
    assert "super-secret-key" not in out  # never print the api key
    assert "base_url:" in out


def test_ssl_label_reflects_verify_off(monkeypatch, capsys):
    monkeypatch.setenv("AIFORGE_LLM_SSL_VERIFY", "false")
    monkeypatch.setenv("AIFORGE_LM_BASE_URL", "https://127.0.0.1:9/v1")
    ct.main(["doer"])
    out = capsys.readouterr().out
    assert "verify=OFF" in out


def test_ssl_label_reflects_ca_bundle(monkeypatch, capsys):
    monkeypatch.setenv("AIFORGE_LLM_CA_BUNDLE", "/etc/ssl/my-ca.pem")
    monkeypatch.setenv("AIFORGE_LM_BASE_URL", "https://127.0.0.1:9/v1")
    ct.main(["doer"])
    out = capsys.readouterr().out
    assert "CA bundle" in out and "verify=ON" in out
