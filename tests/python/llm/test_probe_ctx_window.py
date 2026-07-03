"""Fix B — auto-detect the model's real context window from ``/v1/models``.

Two layers:
  * :func:`health.probe_context_window` — parse ``max_model_len`` (vLLM),
    ``context_length`` / ``loaded_context_length`` (LM Studio), cap 256K.
  * :func:`model_registry.effective_context_window` — resolution order
    explicit > detected > static default, gated by ``AIFORGE_AUTODETECT_CTX``.
"""
from __future__ import annotations

import importlib
import json

import pytest

from aiforge_core.llm import health


class _Resp:
    def __init__(self, body: dict):
        self._b = json.dumps(body).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture(autouse=True)
def _clear(monkeypatch, tmp_path):
    health._CTX_CACHE.clear()
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    for v in ("AIFORGE_LOCAL_CTX_WINDOW", "AIFORGE_AUTODETECT_CTX"):
        monkeypatch.delenv(v, raising=False)
    import aiforge_core.config.runtime_settings as rsmod
    importlib.reload(rsmod)
    yield
    health._CTX_CACHE.clear()
    importlib.reload(importlib.import_module("aiforge_core.config.runtime_settings"))


def _patch_body(monkeypatch, body: dict):
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda *a, **k: _Resp(body))


# ── probe_context_window --------------------------------------------------

def test_probe_reads_max_model_len(monkeypatch):
    _patch_body(monkeypatch, {"data": [{"id": "m", "max_model_len": 262144}]})
    assert health.probe_context_window("http://h/v1") == 262144


def test_probe_reads_context_length(monkeypatch):
    _patch_body(monkeypatch, {"data": [{"id": "m", "context_length": 200000}]})
    assert health.probe_context_window("http://h/v1") == 200000


def test_probe_reads_loaded_context_length(monkeypatch):
    _patch_body(monkeypatch, {"data": [{"loaded_context_length": 131072}]})
    assert health.probe_context_window("http://h/v1") == 131072


def test_probe_caps_at_256k(monkeypatch):
    _patch_body(monkeypatch, {"data": [{"max_model_len": 500000}]})
    assert health.probe_context_window("http://h/v1") == 262144


def test_probe_no_field_returns_none(monkeypatch):
    _patch_body(monkeypatch, {"data": [{"id": "m"}]})
    assert health.probe_context_window("http://h/v1") is None


def test_probe_error_returns_none(monkeypatch):
    def _boom(*a, **k):
        raise OSError("refused")
    monkeypatch.setattr("urllib.request.urlopen", _boom)
    assert health.probe_context_window("http://h/v1") is None


def test_probe_is_cached(monkeypatch):
    calls = {"n": 0}

    def _one(*a, **k):
        calls["n"] += 1
        return _Resp({"data": [{"max_model_len": 262144}]})
    monkeypatch.setattr("urllib.request.urlopen", _one)
    assert health.probe_context_window("http://h/v1") == 262144
    assert health.probe_context_window("http://h/v1") == 262144
    assert calls["n"] == 1               # second call served from cache


def test_negative_result_is_cached(monkeypatch):
    """C2: an unreachable/absent endpoint's None result is cached (long neg
    TTL) so it is NOT re-probed every turn — the second call makes no GET."""
    calls = {"n": 0}

    def _boom(*a, **k):
        calls["n"] += 1
        raise OSError("refused")
    monkeypatch.setattr("urllib.request.urlopen", _boom)
    assert health.probe_context_window("http://down/v1") is None
    assert health.probe_context_window("http://down/v1") is None
    assert calls["n"] == 1               # negative result served from cache


def test_negative_cache_ttl_env_zero_reprobes(monkeypatch):
    """With the neg TTL forced to 0, the negative result is re-probed."""
    monkeypatch.setenv("AIFORGE_CTX_PROBE_NEG_TTL_S", "0")
    calls = {"n": 0}

    def _boom(*a, **k):
        calls["n"] += 1
        raise OSError("refused")
    monkeypatch.setattr("urllib.request.urlopen", _boom)
    assert health.probe_context_window("http://down2/v1") is None
    assert health.probe_context_window("http://down2/v1") is None
    assert calls["n"] == 2               # TTL 0 → re-probed


def test_ctx_timeout_is_short(monkeypatch):
    """C2: the probe GET fails fast (≤1.5s) so a down endpoint can't thrash."""
    for v in ("AIFORGE_HEALTH_TIMEOUT_S",):
        monkeypatch.delenv(v, raising=False)
    assert health._ctx_timeout() <= 1.5


# ── effective_context_window resolution -----------------------------------

def _no_per_model(monkeypatch, base_url="http://d/v1"):
    """No per-model registry value, endpoint base_url = ``base_url``."""
    from aiforge_core.config import model_registry
    monkeypatch.setattr(model_registry, "context_for", lambda *a, **k: 0)

    class _Ep:
        model = "m"
        base_url = "http://d/v1"
    monkeypatch.setattr("aiforge_core.llm.router.resolve", lambda role: _Ep())


def test_explicit_env_wins_over_detected(monkeypatch):
    _no_per_model(monkeypatch)
    monkeypatch.setattr(health, "probe_context_window", lambda url, api_key="": 262144)
    monkeypatch.setenv("AIFORGE_LOCAL_CTX_WINDOW", "100000")
    importlib.reload(importlib.import_module("aiforge_core.config.runtime_settings"))
    from aiforge_core.config import model_registry
    assert model_registry.effective_context_window("doer") == 100000


def test_detected_wins_over_static_default(monkeypatch):
    _no_per_model(monkeypatch)
    monkeypatch.setattr(health, "probe_context_window", lambda url, api_key="": 262144)
    from aiforge_core.config import model_registry
    # No explicit setting → detected (256K) beats the 131072 static default.
    assert model_registry.effective_context_window("doer") == 262144


def test_autodetect_disabled_falls_to_static(monkeypatch):
    _no_per_model(monkeypatch)
    monkeypatch.setattr(health, "probe_context_window", lambda url, api_key="": 262144)
    monkeypatch.setenv("AIFORGE_AUTODETECT_CTX", "0")
    from aiforge_core.config import model_registry
    assert model_registry.effective_context_window("doer") == 131072
