from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from aiforge_core.runtime.tools import browser as br


@pytest.fixture(autouse=True)
def _reset_contexts():
    br._contexts.clear()
    br._console.clear()
    br.set_run_id(None)          # a leaked run id makes drain_console read the
    br._pw_handle = None         # WRONG buffer when tests share a process
    br._browser = None
    yield
    br._contexts.clear()
    br._console.clear()
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


# ── page diagnostics + the commands ui_check drives ─────────────────────────

def _page_with_listeners(monkeypatch):
    """A fake context whose page records the handlers browser attaches."""
    page = MagicMock()
    handlers: dict = {}
    page.on.side_effect = lambda event, fn: handlers.setdefault(event, fn)
    ctx = MagicMock()
    ctx.new_page.return_value = page
    browser_obj = MagicMock()
    browser_obj.new_context.return_value = ctx
    pw = MagicMock()
    pw.chromium.launch.return_value = browser_obj
    monkeypatch.setattr(br, "_playwright_available", lambda: True)
    monkeypatch.setitem(__import__("sys").modules, "playwright.sync_api",
                        MagicMock(sync_playwright=lambda: MagicMock(
                            start=lambda: pw)))
    return page, handlers


def test_listeners_attach_at_context_creation(monkeypatch):
    page, handlers = _page_with_listeners(monkeypatch)
    br._get_context("run-a")
    # Attaching AFTER the first goto would miss every load-time error, which is
    # the class of bug the console capture exists for.
    assert set(handlers) == {"console", "pageerror", "requestfailed"}


def test_console_buffer_collects_and_drains(monkeypatch):
    page, handlers = _page_with_listeners(monkeypatch)
    br._get_context("run-b")
    msg = MagicMock()
    msg.type = "error"
    msg.text = "Uncaught TypeError"
    handlers["console"](msg)
    handlers["pageerror"](RuntimeError("render failed"))
    out = br.drain_console("run-b")
    assert [e["kind"] for e in out] == ["console", "pageerror"]
    # Drained by default, so a second page load reports only its own errors.
    assert br.drain_console("run-b") == []


def test_console_errors_only_filter(monkeypatch):
    br._record("run-c", {"kind": "console", "level": "log", "text": "hi"})
    br._record("run-c", {"kind": "pageerror", "level": "error", "text": "bad"})
    out = br.drain_console("run-c", errors_only=True)
    assert len(out) == 1
    assert out[0]["text"] == "bad"


def test_console_ring_is_bounded(monkeypatch):
    for i in range(br._CONSOLE_RING + 50):
        br._record("run-d", {"kind": "console", "level": "log", "text": str(i)})
    assert len(br._console["run-d"]) == br._CONSOLE_RING


def test_record_never_raises_on_bad_input():
    class _Bad:
        def __str__(self):
            raise ValueError("nope")

    br._record("run-e", {"kind": "console", "text": _Bad()})   # must not raise


def test_console_command_does_not_launch_a_browser(monkeypatch):
    monkeypatch.setattr(br, "_playwright_available", lambda: True)

    def _explode(_rid):
        raise AssertionError("must not create a context")

    monkeypatch.setattr(br, "_get_context", _explode)
    br.set_run_id("console-only")
    br._record("console-only", {"kind": "pageerror", "level": "error", "text": "x"})
    out = br.browse("console")
    assert out["ok"] is True
    assert out["count"] == 1


def test_viewport(monkeypatch):
    page = MagicMock()
    monkeypatch.setattr(br, "_playwright_available", lambda: True)
    monkeypatch.setattr(br, "_get_context", lambda rid: (MagicMock(), page))
    out = br.browse("viewport", width=390, height=844)
    assert out == {"ok": True, "width": 390, "height": 844}
    page.set_viewport_size.assert_called_once_with({"width": 390,
                                                    "height": 844})


def test_viewport_requires_both_dimensions(monkeypatch):
    monkeypatch.setattr(br, "_playwright_available", lambda: True)
    monkeypatch.setattr(br, "_get_context",
                        lambda rid: (MagicMock(), MagicMock()))
    assert br.browse("viewport", width=390)["error"] == "missing_width_or_height"


def test_wait_for_state_selector_and_timeout(monkeypatch):
    page = MagicMock()
    monkeypatch.setattr(br, "_playwright_available", lambda: True)
    monkeypatch.setattr(br, "_get_context", lambda rid: (MagicMock(), page))
    assert br.browse("wait_for", state="networkidle")["waited"] == "state"
    assert br.browse("wait_for", selector="#app")["waited"] == "selector"
    assert br.browse("wait_for", ms=250)["waited"] == "timeout"
    assert br.browse("wait_for", state="teleported")["error"] == "unknown_state"


def test_screenshot_full_page(monkeypatch):
    page = MagicMock()
    page.screenshot.return_value = b"\x89PNG\r\n\x1a\n"
    monkeypatch.setattr(br, "_playwright_available", lambda: True)
    monkeypatch.setattr(br, "_get_context", lambda rid: (MagicMock(), page))
    br.browse("screenshot", full_page=True)
    assert page.screenshot.call_args.kwargs["full_page"] is True


def test_screenshot_bytes_returns_raw_png(monkeypatch):
    page = MagicMock()
    page.screenshot.return_value = b"\x89PNG"
    monkeypatch.setattr(br, "_playwright_available", lambda: True)
    monkeypatch.setattr(br, "_get_context", lambda rid: (MagicMock(), page))
    png, err = br.screenshot_bytes()
    assert png == b"\x89PNG"
    assert err is None


def test_screenshot_bytes_without_playwright(monkeypatch):
    monkeypatch.setattr(br, "_playwright_available", lambda: False)
    png, err = br.screenshot_bytes()
    assert png is None
    assert err == "playwright_missing"


def test_destroy_clears_the_console_buffer():
    br._record("run-f", {"kind": "console", "level": "log", "text": "x"})
    br.destroy_context("run-f")
    assert "run-f" not in br._console


def test_browser_cancelled_requests_are_not_errors():
    br._record("run-g", {"kind": "requestfailed", "level": "error",
                         "text": "net::ERR_CONNECTION_REFUSED"})
    handlers = {}

    class _Req:
        url = "http://x/hmr"
        failure = "net::ERR_ABORTED"

    page = MagicMock()
    page.on.side_effect = lambda event, fn: handlers.setdefault(event, fn)
    br._attach_listeners(page, "run-g")
    handlers["requestfailed"](_Req())
    levels = [e["level"] for e in br.drain_console("run-g")]
    # A navigation-cancelled request would otherwise eat the agent's fix rounds.
    assert levels == ["error", "info"]


def test_a_refused_url_never_launches_a_browser(monkeypatch):
    monkeypatch.setattr(br, "_playwright_available", lambda: True)
    monkeypatch.delenv("AIFORGE_BROWSER_ALLOWLIST", raising=False)
    monkeypatch.delenv("AIFORGE_ALLOW_WEB_FETCH", raising=False)

    def _explode(_rid):
        raise AssertionError("a refused URL must not cost a Chromium launch")

    monkeypatch.setattr(br, "_get_context", _explode)
    out = br.browse("goto", url="http://not-allowed.example/")
    assert out["error"] == "url_not_in_allowlist"
