"""Transport-level retry coverage for aiforge_core.llm.client._post_with_retry.

Validates the retry classifier (5xx/429/URLError = retry; 4xx = abort)
and that successful retry yields the body without escalating providers.
"""
from __future__ import annotations

import io
import urllib.error
from unittest import mock

import pytest

from aiforge_core.llm import client as llm_client
from aiforge_core.llm.types import Endpoint


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch):
    """Pin the retry knobs so tests don't actually sleep."""
    monkeypatch.setenv("AIFORGE_LLM_RETRY_MAX", "3")
    monkeypatch.setenv("AIFORGE_LLM_RETRY_BASE_S", "0.001")
    monkeypatch.setenv("AIFORGE_LLM_RETRY_CAP_S", "0.005")


def _ep() -> Endpoint:
    return Endpoint(
        base_url="http://test",
        api_key="x",
        model="m",
        provider="local",
        role="planner",
        extras={},
    )


def _http_error(code: int, retry_after: str | None = None) -> urllib.error.HTTPError:
    headers = {"Retry-After": retry_after} if retry_after else {}
    return urllib.error.HTTPError(
        url="http://test", code=code, msg="x",
        hdrs=headers, fp=io.BytesIO(b""),
    )


def test_retry_recovers_after_503(monkeypatch):
    calls = {"n": 0}

    def fake_post(ep, payload, t):
        calls["n"] += 1
        if calls["n"] < 3:
            raise _http_error(503)
        return {"ok": True}

    monkeypatch.setattr(llm_client, "_post", fake_post)
    out = llm_client._post_with_retry(
        _ep(), b"{}", 30, role="planner", source="primary",
    )
    assert out == {"ok": True}
    assert calls["n"] == 3


def test_retry_recovers_after_url_error(monkeypatch):
    calls = {"n": 0}

    def fake_post(ep, payload, t):
        calls["n"] += 1
        if calls["n"] < 2:
            raise urllib.error.URLError("conn reset")
        return {"ok": True}

    monkeypatch.setattr(llm_client, "_post", fake_post)
    out = llm_client._post_with_retry(
        _ep(), b"{}", 30, role="planner", source="primary",
    )
    assert out == {"ok": True}
    assert calls["n"] == 2


def test_retry_aborts_on_4xx(monkeypatch):
    calls = {"n": 0}

    def fake_post(ep, payload, t):
        calls["n"] += 1
        raise _http_error(401)

    monkeypatch.setattr(llm_client, "_post", fake_post)
    with pytest.raises(urllib.error.HTTPError) as exc:
        llm_client._post_with_retry(
            _ep(), b"{}", 30, role="planner", source="primary",
        )
    assert exc.value.code == 401
    assert calls["n"] == 1


def test_retry_exhausts_then_raises(monkeypatch):
    calls = {"n": 0}

    def fake_post(ep, payload, t):
        calls["n"] += 1
        raise _http_error(502)

    monkeypatch.setattr(llm_client, "_post", fake_post)
    with pytest.raises(urllib.error.HTTPError) as exc:
        llm_client._post_with_retry(
            _ep(), b"{}", 30, role="planner", source="primary",
        )
    assert exc.value.code == 502
    assert calls["n"] == 3


def test_try_post_uses_retry_then_returns(monkeypatch):
    """End-to-end: _try_post sees a recovered call as success."""
    calls = {"n": 0}

    def fake_post(ep, payload, t):
        calls["n"] += 1
        if calls["n"] < 2:
            raise urllib.error.URLError("flake")
        return {
            "choices": [{"message": {"content": "hi there"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }

    monkeypatch.setattr(llm_client, "_post", fake_post)
    out = llm_client._try_post(
        _ep(), [{"role": "user", "content": "x"}],
        temperature=None, max_tokens=None, top_p=None, extras=None,
        timeout_s=30, role="planner", source="primary",
    )
    assert out is not None
    assert out[0] == "hi there"
    assert calls["n"] == 2


def test_429_honors_retry_after(monkeypatch):
    """429 with Retry-After should still complete (we just sleep less)."""
    calls = {"n": 0}
    sleeps: list[float] = []

    def fake_post(ep, payload, t):
        calls["n"] += 1
        if calls["n"] < 2:
            raise _http_error(429, retry_after="0.001")
        return {"ok": True}

    monkeypatch.setattr(llm_client, "_post", fake_post)
    monkeypatch.setattr(llm_client.time, "sleep",
                        lambda s: sleeps.append(s))
    out = llm_client._post_with_retry(
        _ep(), b"{}", 30, role="planner", source="primary",
    )
    assert out == {"ok": True}
    # First retry slept somewhere between Retry-After (0.001) and cap+jitter.
    assert sleeps and sleeps[0] <= 0.3
