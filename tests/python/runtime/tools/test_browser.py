from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from aiforge_core.runtime.tools import browser as br


@pytest.fixture(autouse=True)
def _reset_contexts():
    br._contexts.clear()
    br._pw_handle = None
    br._browser = None
    yield
    br._contexts.clear()
    br._pw_handle = None
    br._browser = None


def test_playwright_missing_returns_soft_error(monkeypatch):
    monkeypatch.setattr(br, "_playwright_available", lambda: False)
    out = br.browse("goto", url="https://example.com")
    assert out["ok"] is False
    assert out["error"] == "playwright_missing"


def test_unknown_command(monkeypatch):
    monkeypatch.setattr(br, "_playwright_available", lambda: True)
    monkeypatch.setattr(br, "_get_context",
                        lambda rid: (MagicMock(), MagicMock()))
    out = br.browse("teleport")
    assert out["ok"] is False
    assert out["error"] == "unknown_command"


def test_goto_missing_url(monkeypatch):
    monkeypatch.setattr(br, "_playwright_available", lambda: True)
    monkeypatch.setattr(br, "_get_context",
                        lambda rid: (MagicMock(), MagicMock()))
    out = br.browse("goto")
    assert out["ok"] is False
    assert out["error"] == "missing_url"


def test_goto_happy(monkeypatch):
    # Empty allowlist is deny-all under lockdown; opt in to reach example.com.
    monkeypatch.setenv("AIFORGE_ALLOW_WEB_FETCH", "1")
    monkeypatch.setattr(br, "_playwright_available", lambda: True)
    page = MagicMock()
    page.goto.return_value = MagicMock(status=200)
    page.url = "https://example.com/"
    page.title.return_value = "Example Domain"
    monkeypatch.setattr(br, "_get_context", lambda rid: (MagicMock(), page))
    out = br.browse("goto", url="https://example.com")
    assert out["ok"]
    assert out["status"] == 200
    assert out["title"] == "Example Domain"


def test_allowlist_blocks(monkeypatch):
    monkeypatch.setenv("AIFORGE_BROWSER_ALLOWLIST", r"^https://internal\.")
    monkeypatch.setattr(br, "_playwright_available", lambda: True)
    monkeypatch.setattr(br, "_get_context",
                        lambda rid: (MagicMock(), MagicMock()))
    out = br.browse("goto", url="https://attacker.example.com")
    assert out["ok"] is False
    assert out["error"] == "url_not_in_allowlist"


def test_extract_text_caps_size(monkeypatch):
    monkeypatch.setattr(br, "_playwright_available", lambda: True)
    page = MagicMock()
    page.inner_text.return_value = "x" * (40 * 1024)
    monkeypatch.setattr(br, "_get_context", lambda rid: (MagicMock(), page))
    out = br.browse("extract_text")
    assert out["ok"]
    assert out["truncated"] is True
    assert len(out["text"].encode("utf-8")) <= 32 * 1024


def test_click_missing_selector(monkeypatch):
    monkeypatch.setattr(br, "_playwright_available", lambda: True)
    monkeypatch.setattr(br, "_get_context",
                        lambda rid: (MagicMock(), MagicMock()))
    out = br.browse("click")
    assert out["ok"] is False
    assert out["error"] == "missing_selector"


def test_fill_happy(monkeypatch):
    monkeypatch.setattr(br, "_playwright_available", lambda: True)
    page = MagicMock()
    monkeypatch.setattr(br, "_get_context", lambda rid: (MagicMock(), page))
    out = br.browse("fill", selector="#email", text="user@example.com")
    assert out["ok"]
    page.fill.assert_called_once()


def test_close_command(monkeypatch):
    monkeypatch.setattr(br, "_playwright_available", lambda: True)
    out = br.browse("close", _run_id="x")
    assert out["ok"]


def test_screenshot_returns_b64(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_REPO_ROOT", str(tmp_path))
    monkeypatch.setattr(br, "_playwright_available", lambda: True)
    page = MagicMock()
    page.screenshot.return_value = b"\x89PNG\r\n\x1a\n" + b"x" * 100
    monkeypatch.setattr(br, "_get_context", lambda rid: (MagicMock(), page))
    out = br.browse("screenshot", path="shot.png")
    assert out["ok"]
    assert out["path"] == "shot.png"
    assert out["png_b64"]
    assert (tmp_path / "shot.png").exists()


def test_browser_op_failure_soft_errors(monkeypatch):
    monkeypatch.setattr(br, "_playwright_available", lambda: True)
    page = MagicMock()
    page.click.side_effect = RuntimeError("element not visible")
    monkeypatch.setattr(br, "_get_context", lambda rid: (MagicMock(), page))
    out = br.browse("click", selector="#missing")
    assert out["ok"] is False
    assert out["error"] == "browser_op_failed"
    assert "element not visible" in out["detail"]


def test_mouse_click_with_coords(monkeypatch):
    monkeypatch.setattr(br, "_playwright_available", lambda: True)
    page = MagicMock()
    monkeypatch.setattr(br, "_get_context", lambda rid: (MagicMock(), page))
    out = br.browse("mouse_click", x=100, y=200, button="right")
    assert out["ok"]
    assert out["x"] == 100
    assert out["y"] == 200
    page.mouse.click.assert_called_once_with(100, 200, button="right")


def test_mouse_click_missing_coords(monkeypatch):
    monkeypatch.setattr(br, "_playwright_available", lambda: True)
    monkeypatch.setattr(br, "_get_context",
                        lambda rid: (MagicMock(), MagicMock()))
    out = br.browse("mouse_click", x=100)
    assert out["ok"] is False
    assert out["error"] == "missing_x_or_y"


def test_key_press(monkeypatch):
    monkeypatch.setattr(br, "_playwright_available", lambda: True)
    page = MagicMock()
    monkeypatch.setattr(br, "_get_context", lambda rid: (MagicMock(), page))
    out = br.browse("key_press", key="Enter")
    assert out["ok"]
    page.keyboard.press.assert_called_once_with("Enter")


def test_key_press_missing_key(monkeypatch):
    monkeypatch.setattr(br, "_playwright_available", lambda: True)
    monkeypatch.setattr(br, "_get_context",
                        lambda rid: (MagicMock(), MagicMock()))
    out = br.browse("key_press")
    assert out["ok"] is False
    assert out["error"] == "missing_key"


def test_type_text(monkeypatch):
    monkeypatch.setattr(br, "_playwright_available", lambda: True)
    page = MagicMock()
    monkeypatch.setattr(br, "_get_context", lambda rid: (MagicMock(), page))
    out = br.browse("type", text="hello world")
    assert out["ok"]
    assert out["typed_bytes"] == 11
    page.keyboard.type.assert_called_once_with("hello world")


def test_scroll(monkeypatch):
    monkeypatch.setattr(br, "_playwright_available", lambda: True)
    page = MagicMock()
    monkeypatch.setattr(br, "_get_context", lambda rid: (MagicMock(), page))
    out = br.browse("scroll", dy=500)
    assert out["ok"]
    page.mouse.wheel.assert_called_once_with(0, 500)
