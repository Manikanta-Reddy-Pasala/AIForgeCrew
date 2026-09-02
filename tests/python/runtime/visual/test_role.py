from __future__ import annotations

import pytest

from aiforge_core.runtime.visual import _role


class _Ep:
    def __init__(self, model="m", base_url="http://x/v1"):
        self.model = model
        self.base_url = base_url


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("AIFORGE_VISION_ROLE", raising=False)


def _patch(monkeypatch, *, enabled, endpoints=None):
    import aiforge_core.llm.router as router
    import aiforge_core.runtime.vision_detect as vd
    monkeypatch.setattr(vd, "vision_enabled",
                        lambda role, probe=False: enabled.get(role, False))
    eps = endpoints if endpoints is not None else {}
    monkeypatch.setattr(router, "resolve", lambda role: eps.get(role))


def test_own_model_can_see(monkeypatch):
    _patch(monkeypatch, enabled={"chat": True})
    assert _role.vision_role("chat") == ("chat", "")


def test_falls_back_to_vision_role(monkeypatch):
    _patch(monkeypatch, enabled={"vision": True},
           endpoints={"vision": _Ep("qwen/qwen3.8-27b")})
    role, reason = _role.vision_role("chat")
    assert role == "vision"
    assert reason == ""


def test_no_vision_model_gives_actionable_reason(monkeypatch):
    _patch(monkeypatch, enabled={}, endpoints={"vision": None,
                                               "chat": _Ep("coder")})
    role, reason = _role.vision_role("chat")
    assert role is None
    # The reason must name the knob to set — a bare "" was the old behaviour
    # and left the operator with no way to discover vision was never wired.
    assert "AIFORGE_VISION_MODEL" in reason or "AIFORGE_VISION_BASE_URL" in reason


def test_configured_role_that_rejects_images(monkeypatch):
    _patch(monkeypatch, enabled={}, endpoints={"vision": _Ep("text-only-7b"),
                                               "chat": _Ep("coder")})
    role, reason = _role.vision_role("chat")
    assert role is None
    assert "text-only-7b" in reason


def test_vision_role_pointing_at_itself(monkeypatch):
    monkeypatch.setenv("AIFORGE_VISION_ROLE", "chat")
    _patch(monkeypatch, enabled={}, endpoints={"chat": _Ep("coder")})
    role, reason = _role.vision_role("chat")
    assert role is None
    assert "AIFORGE_VISION_ROLE" in reason


def test_probe_exception_is_not_a_verdict(monkeypatch):
    import aiforge_core.llm.router as router
    import aiforge_core.runtime.vision_detect as vd

    def _boom(role, probe=False):
        if role == "chat":
            raise RuntimeError("endpoint down")
        return True

    monkeypatch.setattr(vd, "vision_enabled", _boom)
    monkeypatch.setattr(router, "resolve", lambda role: _Ep("vlm"))
    assert _role.vision_role("chat")[0] == "vision"


def test_configured_vlm_wins_over_a_role_blind_global_override(monkeypatch):
    # vision_detect honours a GLOBAL `vision_capable` setting that is not
    # per-role: with it on, the text-only chat model claims it can see. The
    # dedicated VLM the operator wired up must still be the one consulted.
    import aiforge_core.llm.router as router
    import aiforge_core.runtime.vision_detect as vd
    monkeypatch.setattr(vd, "vision_enabled", lambda role, probe=False: True)
    monkeypatch.setattr(router, "resolve", lambda role: _Ep(
        "qwen/qwen3-coder-next" if role == "chat" else "qwen/qwen3.8-27b"))
    assert _role.vision_role("chat")[0] == "vision"


def test_an_unconfigured_vision_role_does_not_shadow_a_capable_chat_model(
        monkeypatch):
    import aiforge_core.llm.router as router
    import aiforge_core.runtime.vision_detect as vd
    monkeypatch.setattr(vd, "vision_enabled",
                        lambda role, probe=False: role == "chat")
    # "default" is the placeholder an unconfigured role resolves to.
    monkeypatch.setattr(router, "resolve",
                        lambda role: _Ep("vlm-chat" if role == "chat"
                                         else "default"))
    assert _role.vision_role("chat") == ("chat", "")
