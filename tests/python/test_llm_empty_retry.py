"""Empty / think-only 200-OK handling.

Reasoning models (qwen3-coder) intermittently return a 200-OK whose content is
empty or a bare ``<think>…</think>`` block with no answer after it. That used to
either fall straight to the (non-existent) fallback provider — surfacing as
``llm.exhausted`` on a single-model NUC — or, on the ADK team path, pass raw
chain-of-thought through as the agent's answer. These tests pin the fixes:

  * ``_strip_think`` removes think blocks (closed and unclosed).
  * ``_extract_text`` collapses a think-only content to "" and falls back to
    the reasoning channel.
  * ``_try_post`` re-posts the SAME endpoint AIFORGE_LLM_EMPTY_RETRIES times on
    garbage before giving up, and returns the first real answer.
"""
from __future__ import annotations

from aiforge_core.llm import client as c


def test_strip_think_closed_block():
    assert c._strip_think("<think>reasoning</think>Answer.") == "Answer."


def test_strip_think_only_collapses_to_empty():
    assert c._strip_think("<think>ran out of budget thinking</think>") == ""


def test_strip_think_unclosed_opener_drops_to_end():
    assert c._strip_think("partial <think>never closed to EOF") == "partial"


def test_strip_think_plain_untouched():
    assert c._strip_think("just a normal answer") == "just a normal answer"


def test_extract_text_thinkonly_falls_back_to_reasoning():
    body = {"choices": [{"message": {
        "content": "<think>x</think>",
        "reasoning_content": "the real answer",
    }}]}
    assert c._extract_text(body) == "the real answer"


def test_extract_text_both_empty_is_garbage():
    body = {"choices": [{"message": {
        "content": "<think>x</think>", "reasoning_content": "",
    }}]}
    txt = c._extract_text(body)
    assert txt == "" and c._is_garbage(txt)


def _msg_body(text: str) -> dict:
    return {"choices": [{"message": {"content": text}}]}


class _Ep:
    provider = "test"
    model = "test-model"
    base_url = "http://127.0.0.1:0"


def test_try_post_retries_same_endpoint_until_real_answer(monkeypatch):
    # First two posts are think-only garbage, third has a real answer.
    monkeypatch.setenv("AIFORGE_LLM_EMPTY_RETRIES", "2")
    monkeypatch.setattr(c.time, "sleep", lambda *_: None)  # no real backoff
    seq = iter([
        _msg_body("<think>still thinking</think>"),
        _msg_body(""),
        _msg_body("final answer"),
    ])
    calls = {"n": 0}

    def fake_post_with_retry(ep, payload, timeout_s, *, role, source):
        calls["n"] += 1
        return next(seq)

    monkeypatch.setattr(c, "_post_with_retry", fake_post_with_retry)
    monkeypatch.setattr(c, "_build_body", lambda *a, **k: b"{}")

    out = c._try_post(
        _Ep(), [{"role": "user", "content": "hi"}],
        temperature=None, max_tokens=None, top_p=None, extras=None,
        timeout_s=5, role="chat", source="primary",
    )
    assert out is not None and out[0] == "final answer"
    assert calls["n"] == 3   # two garbage retries + the good one


def test_try_post_gives_up_after_all_retries_garbage(monkeypatch):
    monkeypatch.setenv("AIFORGE_LLM_EMPTY_RETRIES", "2")
    monkeypatch.setattr(c.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    def fake_post_with_retry(ep, payload, timeout_s, *, role, source):
        calls["n"] += 1
        return _msg_body("<think>never answers</think>")

    monkeypatch.setattr(c, "_post_with_retry", fake_post_with_retry)
    monkeypatch.setattr(c, "_build_body", lambda *a, **k: b"{}")

    out = c._try_post(
        _Ep(), [{"role": "user", "content": "hi"}],
        temperature=None, max_tokens=None, top_p=None, extras=None,
        timeout_s=5, role="chat", source="primary",
    )
    assert out is None
    assert calls["n"] == 3   # retries+1, all garbage → None (caller escalates)


def test_headroom_bypass_rescues_empty_proxy_response(monkeypatch):
    """When the primary is the headroom proxy and it returns garbage, the
    IDENTICAL request against the raw upstream model must rescue the call
    (the observed proxy-induced empty-content tool_calls failure)."""
    from aiforge_core.llm.types import Endpoint
    from aiforge_core.llm import headroom as hr

    monkeypatch.setenv("AIFORGE_HEADROOM", "1")
    monkeypatch.setenv("AIFORGE_HEADROOM_URL", "http://127.0.0.1:8787")
    monkeypatch.setenv("AIFORGE_HEADROOM_UPSTREAM", "http://127.0.0.1:1234")

    proxied = Endpoint(
        base_url="http://127.0.0.1:8787/v1", api_key="x",
        model="qwen", provider="openai_compatible", role="doer", extras=None,
    )
    monkeypatch.setattr(c, "resolve", lambda role: proxied)
    monkeypatch.setattr(c, "escalate", lambda *a, **k: None)
    monkeypatch.setattr(c, "fallback", lambda role: None)
    monkeypatch.setattr(c, "_build_body", lambda *a, **k: b"{}")
    monkeypatch.setattr(c, "_estimate_tokens", lambda *a, **k: 10)

    seen = {}

    def fake_try_post(ep, messages, **kw):
        seen[kw["source"]] = ep.base_url
        if kw["source"] == "primary":
            return None                       # proxy → garbage
        if kw["source"] == "headroom_bypass":
            assert ep.base_url == "http://127.0.0.1:1234/v1"  # raw upstream
            return ("real answer from model", {})
        return None

    monkeypatch.setattr(c, "_try_post", fake_try_post)

    out = c._complete_impl("doer", [{"role": "user", "content": "hi"}])
    assert out == "real answer from model"
    assert seen.get("headroom_bypass") == "http://127.0.0.1:1234/v1"
