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


def test_empty_json_container_is_not_garbage():
    """The learner replies '[]' (nothing durable to record) — a valid answer,
    NOT garbage. It must pass through on attempt 1 (no coax, no warning spam)."""
    assert c._is_garbage("[]") is False
    assert c._is_garbage("{}") is False
    # genuinely empty / fragments are still garbage
    assert c._is_garbage("") is True
    assert c._is_garbage("  ") is True
    assert c._is_garbage("<tool_call>") is True


def test_learner_empty_list_returns_first_attempt(monkeypatch):
    """A model that answers '[]' must NOT trigger the 3× empty-retry loop."""
    posts: list = []

    def _fake_post(ep, payload, timeout_s, *, role, source):
        posts.append(1)
        return _body("[]")
    monkeypatch.setattr(c, "_post_with_retry", _fake_post)
    monkeypatch.setattr(c, "_record_usage", lambda *a, **k: None)
    out = c._try_post(
        _ep(), [{"role": "user", "content": "distil facts"}],
        temperature=0.0, max_tokens=800, top_p=None, extras=None,
        timeout_s=30, role="learner", source="primary")
    assert out is not None and out[0] == "[]"
    assert len(posts) == 1        # single post, no empty-retry loop


def test_empty_retry_widens_max_tokens_progressively(monkeypatch):
    """Each empty-retry gives the (still-thinking) model MORE room: ×2, ×3, …
    so we keep trying to get a real answer instead of giving up."""
    posts: list[dict] = []

    def _fake_post(ep, payload, timeout_s, *, role, source):
        posts.append(json.loads(payload.decode()))
        return _body("")                     # always empty → exhaust retries
    monkeypatch.setattr(c, "_post_with_retry", _fake_post)
    monkeypatch.setattr(c, "_record_usage", lambda *a, **k: None)
    monkeypatch.setenv("AIFORGE_LLM_EMPTY_RETRIES", "3")
    out = c._try_post(_ep(), [{"role": "user", "content": "hi"}],
                      temperature=0.0, max_tokens=4096, top_p=None, extras=None,
                      timeout_s=30, role="doer", source="primary")
    assert out is None
    assert len(posts) == 4                   # 1 initial + 3 retries (default 3)
    mts = [p["max_tokens"] for p in posts]
    assert mts == [4096, 8192, 12288, 16384]  # verbatim, then ×2, ×3, ×4


def test_default_empty_retries_is_three(monkeypatch):
    posts: list = []
    monkeypatch.setattr(c, "_post_with_retry",
                        lambda *a, **k: (posts.append(1), _body(""))[1])
    monkeypatch.setattr(c, "_record_usage", lambda *a, **k: None)
    monkeypatch.delenv("AIFORGE_LLM_EMPTY_RETRIES", raising=False)
    c._try_post(_ep(), [{"role": "user", "content": "hi"}],
                temperature=None, max_tokens=None, top_p=None, extras=None,
                timeout_s=30, role="chat", source="primary")
    assert len(posts) == 4                   # default now 3 retries → 4 posts


def test_all_empty_returns_none(monkeypatch):
    monkeypatch.setattr(c, "_post_with_retry",
                        lambda *a, **k: _body(""))       # always empty
    monkeypatch.setattr(c, "_record_usage", lambda *a, **k: None)
    monkeypatch.setenv("AIFORGE_LLM_EMPTY_RETRIES", "2")
    out = c._try_post(_ep(), [{"role": "user", "content": "hi"}],
                      temperature=None, max_tokens=None, top_p=None, extras=None,
                      timeout_s=30, role="chat", source="primary")
    assert out is None
