"""Screenshot observations carry pixels when the chat model can see."""
from __future__ import annotations

import base64

from aiforge_core.runtime.chat_agent import _obs_vision
from aiforge_core.runtime.chat_agent._tools import _pipeline


def test_observation_content_attaches_image(tmp_path):
    png = tmp_path / "s.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    content = _obs_vision.observation_content(
        '{"ok": true}', {"pixels": True, "screenshot": str(png)})
    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    assert any(p.get("type") == "image_url" for p in content)


def test_observation_content_stays_text_without_pixels():
    assert _obs_vision.observation_content("hi", {"ok": True}) == "OBSERVATION: hi"


def test_screenshot_attaches_pixels_when_chat_can_see(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    import aiforge_core.runtime.tools.browser as real
    monkeypatch.setattr(real, "browse",
                        lambda command, **kw: {
                            "ok": True,
                            "png_b64": base64.b64encode(png).decode(),
                            "truncated": False})
    monkeypatch.setattr(_obs_vision, "chat_can_see", lambda role="chat": True)

    audited = []

    def _boom(*a, **kw):
        audited.append(1)
        raise AssertionError("default path must not call a second vision model")

    from aiforge_core.runtime import visual
    monkeypatch.setattr(visual, "audit_image", _boom)
    out = _pipeline._t_browse({"command": "screenshot"}, ".")
    assert out.get("pixels") is True
    assert out.get("see") is True
    assert out.get("screenshot")
    assert "png_b64" not in out
    assert audited == []


def test_screenshot_still_audits_when_chat_cannot_see(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    import aiforge_core.runtime.tools.browser as real
    monkeypatch.setattr(real, "browse",
                        lambda command, **kw: {
                            "ok": True,
                            "png_b64": base64.b64encode(png).decode(),
                            "truncated": False})
    monkeypatch.setattr(_obs_vision, "chat_can_see", lambda role="chat": False)
    from aiforge_core.runtime import visual
    monkeypatch.setattr(visual, "audit_image",
                        lambda path, role="chat": {
                            "ok": True, "text": "SCREEN: x",
                            "vision_role": "vision"})
    out = _pipeline._t_browse({"command": "screenshot"}, ".")
    assert out["audit"] == "SCREEN: x"
    assert out.get("pixels") is not True
