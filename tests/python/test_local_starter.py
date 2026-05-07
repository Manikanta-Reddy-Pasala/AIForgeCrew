"""Tests for the SSH-based auto-start of local mlx-lm endpoints."""
from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from aiforge_core.runtime import local_starter as ls
from aiforge_core.runtime import local_probe as lp


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts with empty caches + clean env."""
    ls.reset()
    lp._CACHE.clear()
    import os as _os
    for k in list(_os.environ.keys()):
        if k.startswith(("AIFORGE_LMS_", "AIFORGE_CLAUDE_HOST")):
            monkeypatch.delenv(k, raising=False)


def _proc_ok() -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


def _proc_fail(rc: int = 1) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout="",
                                       stderr="boom")


def test_disabled_skips_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIFORGE_LMS_AUTOSTART_DISABLE", "1")
    monkeypatch.setenv("AIFORGE_LMS_HOST", "user@h")
    with patch("subprocess.run", side_effect=AssertionError("called")):
        assert ls.try_start("http://x:1234/v1") is False


def test_no_host_returns_false() -> None:
    """Even if auto-start is enabled, no SSH target = no-op."""
    with patch("subprocess.run", side_effect=AssertionError("called")):
        assert ls.try_start("http://x:1234/v1") is False


def test_success_path_keeps_local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIFORGE_LMS_HOST", "user@studio")
    monkeypatch.setenv("AIFORGE_LMS_WARMUP_S", "0")  # skip the sleep

    class _Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): pass

    with patch("subprocess.run", return_value=_proc_ok()) as run_mock, \
         patch("urllib.request.urlopen", return_value=_Resp()), \
         patch("time.sleep"):
        assert ls.try_start("http://x:1234/v1") is True

    # SSH was actually invoked with the expected shape.
    args = run_mock.call_args[0][0]
    assert args[0] == "ssh"
    assert "user@studio" in args
    joined = " ".join(args)
    assert "lms server start" in joined
    assert "lms load" in joined
    # Context-length is the contractual minimum (64K) — anything smaller
    # truncates Doer prompts on multi-file tickets.
    assert "--context-length 65536" in joined


def test_ctx_env_override_takes_effect(monkeypatch: pytest.MonkeyPatch) -> None:
    """Operator can raise ctx via AIFORGE_LMS_CTX without touching code."""
    monkeypatch.setenv("AIFORGE_LMS_HOST", "user@studio")
    monkeypatch.setenv("AIFORGE_LMS_WARMUP_S", "0")
    monkeypatch.setenv("AIFORGE_LMS_CTX", "131072")

    class _Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): pass

    with patch("subprocess.run", return_value=_proc_ok()) as run_mock, \
         patch("urllib.request.urlopen", return_value=_Resp()), \
         patch("time.sleep"):
        ls.try_start("http://x:1234/v1")
    joined = " ".join(run_mock.call_args[0][0])
    assert "--context-length 131072" in joined


def test_ctx_env_below_floor_clamps_to_minimum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setting AIFORGE_LMS_CTX below 64K must clamp up — never accept a
    value that risks the original 4K-truncation bug."""
    monkeypatch.setenv("AIFORGE_LMS_HOST", "user@studio")
    monkeypatch.setenv("AIFORGE_LMS_WARMUP_S", "0")
    monkeypatch.setenv("AIFORGE_LMS_CTX", "8192")  # try to undercut

    class _Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): pass

    with patch("subprocess.run", return_value=_proc_ok()) as run_mock, \
         patch("urllib.request.urlopen", return_value=_Resp()), \
         patch("time.sleep"):
        ls.try_start("http://x:1234/v1")
    joined = " ".join(run_mock.call_args[0][0])
    assert "--context-length 65536" in joined
    assert "--context-length 8192" not in joined


def test_load_failure_falls_through(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIFORGE_LMS_HOST", "user@studio")
    monkeypatch.setenv("AIFORGE_LMS_WARMUP_S", "0")

    with patch("subprocess.run", return_value=_proc_fail()), \
         patch("time.sleep"):
        assert ls.try_start("http://x:1234/v1") is False


def test_ssh_timeout_falls_through(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIFORGE_LMS_HOST", "user@studio")

    with patch("subprocess.run",
               side_effect=subprocess.TimeoutExpired(cmd="ssh", timeout=10)):
        assert ls.try_start("http://x:1234/v1") is False


def test_post_warmup_probe_dead_returns_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SSH succeeds but the endpoint still doesn't answer — caller falls
    back to cloud."""
    monkeypatch.setenv("AIFORGE_LMS_HOST", "user@studio")
    monkeypatch.setenv("AIFORGE_LMS_WARMUP_S", "0")
    import urllib.error

    with patch("subprocess.run", return_value=_proc_ok()), \
         patch("urllib.request.urlopen",
               side_effect=urllib.error.URLError("still dead")), \
         patch("time.sleep"):
        assert ls.try_start("http://x:1234/v1") is False


def test_only_one_attempt_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """Avoid hammering Mac Studio if the first attempt failed — second
    call inside the same process returns the cached verdict without
    a fresh SSH."""
    monkeypatch.setenv("AIFORGE_LMS_HOST", "user@studio")
    monkeypatch.setenv("AIFORGE_LMS_WARMUP_S", "0")
    calls = {"n": 0}

    def _track(*args, **kw):
        calls["n"] += 1
        return _proc_fail()

    with patch("subprocess.run", side_effect=_track), patch("time.sleep"):
        assert ls.try_start("http://x:1234/v1") is False
        assert ls.try_start("http://x:1234/v1") is False
    assert calls["n"] == 1


# ─── End-to-end with maybe_substitute_primary ─────────────────────────


def test_dead_local_recovered_via_autostart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Probe sees dead → starter SSH succeeds → re-probe alive →
    primary cfg preserved (no cloud swap)."""
    monkeypatch.setenv("AIFORGE_LMS_HOST", "user@studio")
    monkeypatch.setenv("AIFORGE_LMS_WARMUP_S", "0")
    import urllib.error

    class _Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): pass

    # First urlopen call (initial probe) raises — dead.
    # Second (post-warmup) returns 200 — alive.
    side_effects = [urllib.error.URLError("dead"), _Resp()]

    cfg = {"model_id": "openai//Users/foo", "api_base": "http://127.0.0.1:1234/v1",
           "api_key": "lm-studio"}
    with patch("subprocess.run", return_value=_proc_ok()), \
         patch("urllib.request.urlopen", side_effect=side_effects), \
         patch("time.sleep"):
        out = lp.maybe_substitute_primary("doer", cfg)
    assert out is cfg


def test_dead_local_autostart_fails_falls_back_to_cloud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Probe dead → starter SSH 1xx → still dead → cfg swapped to
    cloud default."""
    monkeypatch.setenv("AIFORGE_LMS_HOST", "user@studio")
    monkeypatch.setenv("AIFORGE_LMS_WARMUP_S", "0")
    monkeypatch.setenv("OLLAMA_CLOUD_API_KEY", "k")
    import urllib.error

    cfg = {"model_id": "openai//Users/foo", "api_base": "http://127.0.0.1:1234/v1",
           "api_key": "lm-studio"}
    with patch("subprocess.run", return_value=_proc_fail()), \
         patch("urllib.request.urlopen",
               side_effect=urllib.error.URLError("dead")), \
         patch("time.sleep"):
        out = lp.maybe_substitute_primary("doer", cfg)
    assert out is not cfg
    assert out.get("_provider") == "ollama_cloud"
