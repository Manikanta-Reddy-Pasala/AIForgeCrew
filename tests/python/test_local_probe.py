"""Tests for :mod:`aiforge_core.runtime.local_probe`.

Two layers exercised:

* :func:`is_alive` — the raw HTTP probe with cache + env knobs.
* :func:`maybe_substitute_primary` — the build-time decision that
  swaps a dead local primary for a cloud default.
"""
from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from aiforge_core.runtime import local_probe as lp


@pytest.fixture(autouse=True)
def _clear_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts with a fresh cache + clean env."""
    lp._CACHE.clear()
    for k in list(__import__("os").environ.keys()):
        if k.startswith("AIFORGE_LOCAL_PROBE_"):
            monkeypatch.delenv(k, raising=False)


def test_alive_when_endpoint_responds() -> None:
    class _Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): pass

    with patch("urllib.request.urlopen", return_value=_Resp()):
        assert lp.is_alive("http://127.0.0.1:1234/v1") is True


def test_dead_on_connection_refused() -> None:
    import urllib.error
    with patch("urllib.request.urlopen",
               side_effect=urllib.error.URLError("Connection refused")):
        assert lp.is_alive("http://127.0.0.1:1234/v1") is False


def test_dead_on_timeout() -> None:
    with patch("urllib.request.urlopen", side_effect=TimeoutError("slow")):
        assert lp.is_alive("http://127.0.0.1:1234/v1") is False


def test_empty_api_base_dead() -> None:
    assert lp.is_alive("") is False


def test_disable_env_forces_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    """``AIFORGE_LOCAL_PROBE_DISABLE=1`` makes the probe always say
    alive — useful when an operator knows the endpoint is up but the
    network is doing something funky (firewall, IPv6 surprise)."""
    monkeypatch.setenv("AIFORGE_LOCAL_PROBE_DISABLE", "1")
    # urlopen would fail here but the probe never calls it.
    with patch("urllib.request.urlopen", side_effect=AssertionError("called")):
        assert lp.is_alive("http://anything") is True


def test_cache_hits_skip_http(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIFORGE_LOCAL_PROBE_TTL_S", "60")
    import urllib.error
    calls = {"n": 0}

    def _raise(*a, **kw):
        calls["n"] += 1
        raise urllib.error.URLError("dead")

    with patch("urllib.request.urlopen", side_effect=_raise):
        assert lp.is_alive("http://1") is False
        assert lp.is_alive("http://1") is False
        assert lp.is_alive("http://1") is False
    assert calls["n"] == 1


# ─── maybe_substitute_primary ────────────────────────────────────────


def _make_cfg(api_base: str) -> dict:
    return {
        "model_id": "openai//Users/foo/Qwen3-Coder",
        "api_base": api_base,
        "api_key": "lm-studio",
    }


def test_substitute_skips_remote_endpoint() -> None:
    """An ollama_cloud primary isn't probed — those have
    their own SLAs. Only the local mlx-lm endpoint can plausibly be
    off because the operator forgot to start LM Studio."""
    cfg = _make_cfg("https://ollama.com/v1")
    with patch("urllib.request.urlopen", side_effect=AssertionError("probed")):
        out = lp.maybe_substitute_primary("doer", cfg)
    assert out is cfg


def test_substitute_keeps_alive_local() -> None:
    class _Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): pass

    cfg = _make_cfg("http://127.0.0.1:1234/v1")
    with patch("urllib.request.urlopen", return_value=_Resp()):
        out = lp.maybe_substitute_primary("doer", cfg)
    assert out is cfg


def test_substitute_is_noop_for_dead_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """openai_compatible is the only provider now — there is no cloud
    default to swap to, so maybe_substitute_primary is a pure no-op and
    returns the original cfg even when the local endpoint is dead. The
    per-call EscalatingLlm retry chain surfaces the error instead."""
    monkeypatch.setenv("AIFORGE_ESCALATE_DISABLE", "0")
    cfg = _make_cfg("http://127.0.0.1:1234/v1")
    out = lp.maybe_substitute_primary("doer", cfg)
    assert out is cfg


def test_substitute_keeps_dead_when_no_cloud_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No cloud default configured → the helper has nothing to swap to.
    Caller falls back to the chain's per-call rescue path."""
    monkeypatch.setenv("AIFORGE_ESCALATE_DISABLE", "1")
    cfg = _make_cfg("http://127.0.0.1:1234/v1")
    out = lp.maybe_substitute_primary("doer", cfg)
    assert out is cfg
