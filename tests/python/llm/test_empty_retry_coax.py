"""On an empty-response retry, the client must COAX a direct answer — append
/no_think and widen max_tokens — not re-post the identical body (a reasoning
model that thinks itself empty would just repeat)."""
from __future__ import annotations
import json
import types
import pytest

from aiforge_core.llm import client as c


def _ep():
    return types.SimpleNamespace(model="qwen35-122b-reasoning", provider="test",
                                 extras={}, base_url="http://x/v1")


def _body(content):
    return {"choices": [{"message": {"content": content, "reasoning_content": ""}}]}


def test_empty_then_coaxed_retry(monkeypatch):
    posts: list[dict] = []

    def _fake_post(ep, payload, timeout_s, *, role, source):
        posts.append(json.loads(payload.decode()))
        # first post → empty (reasoning-only); second → real answer
        return _body("" if len(posts) == 1 else "System log tickets are tracked…")

    monkeypatch.setattr(c, "_post_with_retry", _fake_post)
    monkeypatch.setattr(c, "_record_usage", lambda *a, **k: None)

    out = c._try_post(
        _ep(), [{"role": "system", "content": "s"},
                {"role": "user", "content": "explain sys logs"}],
        temperature=0.0, max_tokens=4096, top_p=None, extras=None,
        timeout_s=30, role="learner", source="primary")

    assert out is not None and "System log tickets" in out[0]
    assert len(posts) == 2
    # attempt 1: verbatim, no coaxing
    assert posts[0]["messages"][-1]["content"] == "explain sys logs"
    assert posts[0]["max_tokens"] == 4096
    # attempt 2: /no_think appended + max_tokens widened
    assert posts[1]["messages"][-1]["content"].endswith("/no_think")
    assert posts[1]["max_tokens"] == 8192


def test_all_empty_returns_none(monkeypatch):
    monkeypatch.setattr(c, "_post_with_retry",
                        lambda *a, **k: _body(""))       # always empty
    monkeypatch.setattr(c, "_record_usage", lambda *a, **k: None)
    monkeypatch.setenv("AIFORGE_LLM_EMPTY_RETRIES", "2")
    out = c._try_post(_ep(), [{"role": "user", "content": "hi"}],
                      temperature=None, max_tokens=None, top_p=None, extras=None,
                      timeout_s=30, role="chat", source="primary")
    assert out is None
