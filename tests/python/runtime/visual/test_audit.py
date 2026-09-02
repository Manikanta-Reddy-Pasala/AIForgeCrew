from __future__ import annotations

import pytest

from aiforge_core.runtime.visual import _audit


@pytest.fixture
def png(tmp_path):
    p = tmp_path / "shot.png"
    p.write_bytes(
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)     # magic bytes are enough
    return str(p)


def _vision(monkeypatch, role="vision", reason=""):
    monkeypatch.setattr(_audit, "vision_role", lambda r: (role, reason))


def _complete(monkeypatch, out="SCREEN: a login form\nISSUES:\n- none\nTEXT: Sign in",
              reasoning=""):
    """Fake the RAW message: the audit reads ``content`` itself so a thinking
    model's ``reasoning_content`` can never be served up as the answer."""
    seen = {}

    def _c(role, messages, **kw):
        seen["role"] = role
        seen["messages"] = messages
        seen["max_tokens"] = kw.get("max_tokens")
        return {"role": "assistant", "content": out,
                "reasoning_content": reasoning}

    import aiforge_core.llm.client as client
    monkeypatch.setattr(client, "complete_raw", _c)
    return seen


def test_audit_returns_text(monkeypatch, png):
    _vision(monkeypatch)
    seen = _complete(monkeypatch)
    out = _audit.audit_image(png)
    assert out["ok"] is True
    assert "login form" in out["text"]
    assert out["vision_role"] == "vision"
    assert seen["role"] == "vision"


def test_audit_prompt_asks_for_the_fixed_schema(monkeypatch, png):
    _vision(monkeypatch)
    seen = _complete(monkeypatch)
    _audit.audit_image(png)
    text = seen["messages"][0]["content"][0]["text"]
    for marker in ("SCREEN:", "ISSUES:", "TEXT:", "- none"):
        assert marker in text


def test_audit_sends_the_image(monkeypatch, png):
    _vision(monkeypatch)
    seen = _complete(monkeypatch)
    _audit.audit_image(png)
    kinds = [b.get("type") for b in seen["messages"][0]["content"]]
    assert "image_url" in kinds


def test_no_vision_model_reports_the_reason(monkeypatch, png):
    _vision(monkeypatch, role=None, reason="no vision model configured: …")
    out = _audit.audit_image(png)
    assert out["ok"] is False
    assert out["error"] == "no_vision_model"
    assert out["hint"].startswith("no vision model configured")


def test_missing_file(monkeypatch, tmp_path):
    _vision(monkeypatch)
    out = _audit.audit_image(str(tmp_path / "nope.png"))
    assert out["error"] == "image_not_found"


def test_call_failure_is_soft(monkeypatch, png):
    _vision(monkeypatch)
    import aiforge_core.llm.client as client

    def _boom(*a, **kw):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(client, "complete_raw", _boom)
    out = _audit.audit_image(png)
    assert out["ok"] is False
    assert out["error"] == "vision_call_failed"


def test_empty_reply_names_the_thinking_model_trap(monkeypatch, png):
    _vision(monkeypatch)
    _complete(monkeypatch, out="   ")
    out = _audit.audit_image(png)
    assert out["error"] == "vision_empty_reply"
    assert "AIFORGE_UI_AUDIT_MAX_TOKENS" in out["hint"]


def test_chain_of_thought_is_never_served_as_the_audit(monkeypatch, png):
    # A thinking VLM that spends its budget reasoning returns empty content
    # with the trace in reasoning_content. Presenting that as the audit reads
    # like a description of the screen while describing nothing.
    _vision(monkeypatch)
    _complete(monkeypatch, out="",
              reasoning="The user wants a yes/no answer. Let me look carefully…")
    out = _audit.audit_image(png)
    assert out["ok"] is False
    assert out["error"] == "vision_empty_reply"


def test_inline_think_block_is_stripped(monkeypatch, png):
    _vision(monkeypatch)
    _complete(monkeypatch, out="<think>hmm, the sidebar…</think>SCREEN: ok")
    out = _audit.audit_image(png)
    assert out["text"] == "SCREEN: ok"


def test_max_tokens_is_generous_by_default(monkeypatch, png):
    _vision(monkeypatch)
    seen = _complete(monkeypatch)
    _audit.audit_image(png)
    # A thinking VLM spends the budget on reasoning before the answer.
    assert seen["max_tokens"] >= 500


def test_max_tokens_env_override(monkeypatch, png):
    monkeypatch.setenv("AIFORGE_UI_AUDIT_MAX_TOKENS", "1500")
    _vision(monkeypatch)
    seen = _complete(monkeypatch)
    _audit.audit_image(png)
    assert seen["max_tokens"] == 1500


def test_ask_image_carries_the_question(monkeypatch, png):
    _vision(monkeypatch)
    seen = _complete(monkeypatch, out="Yes, it overlaps.")
    out = _audit.ask_image(png, "does the sidebar overlap?")
    assert out["ok"] is True
    assert "does the sidebar overlap?" in seen["messages"][0]["content"][0]["text"]


def test_ask_image_requires_a_question(monkeypatch, png):
    _vision(monkeypatch)
    assert _audit.ask_image(png, "  ")["error"] == "missing_question"
