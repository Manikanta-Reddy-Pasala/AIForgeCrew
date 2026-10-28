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


# ── request guard: every request, not just goto, answers to the allowlist ──

@pytest.fixture
def _locked(monkeypatch):
    """No web fetching, no operator allowlist: only local hosts pass."""
    monkeypatch.delenv("AIFORGE_ALLOW_WEB_FETCH", raising=False)
    monkeypatch.delenv("AIFORGE_BROWSER_ALLOWLIST", raising=False)
    br._vouched.clear()
    yield
    br._vouched.clear()


@pytest.mark.parametrize("url", [
    "http://localhost:5173/app.js", "http://127.0.0.1:8080/api",
    "http://192.168.1.20:3000/", "data:image/png;base64,AAAA",
    "blob:http://localhost:5173/abc", "about:blank"])
def test_request_guard_allows_local_and_in_process(_locked, url):
    assert br._request_ok("r", url) is True


@pytest.mark.parametrize("url", [
    "https://fonts.googleapis.com/css2", "https://evil.example/collect?d=1",
    "http://169.254.169.254/latest/meta-data/", "wss://evil.example/socket",
    "https://www.google.com/search?q=secret", ""])
def test_request_guard_blocks_the_open_web(_locked, url):
    assert br._request_ok("r", url) is False


def test_request_guard_follows_the_operator_allowlist(_locked, monkeypatch):
    from aiforge_core.net import egress
    monkeypatch.setattr(egress, "host_allowed", lambda u: "cdn.allowed.dev" in u)
    monkeypatch.setattr(egress, "hard_off", lambda: False)
    monkeypatch.setenv("AIFORGE_BROWSER_ALLOWLIST", "cdn.allowed.dev")
    assert br._request_ok("r", "https://cdn.allowed.dev/lib.js") is True
    assert br._request_ok("r", "https://cdn.other.dev/lib.js") is False


def test_a_vouch_reaches_callbacks_for_its_run_and_dies_with_the_call(_locked):
    # Playwright callbacks and proxy threads cannot see the ContextVar, so the
    # grant is mirrored per run — and must not outlive reset_allow.
    import threading

    def in_callback(run_id):        # a fresh thread: no ContextVar, like a
        out = []                    # Playwright callback or a proxy thread
        t = threading.Thread(target=lambda: out.append(
            br._request_ok(run_id, "http://devbox:5173/main.js")))
        t.start()
        t.join()
        return out[0]

    token = br.allow_hosts(["devbox"], run_id="r1")
    try:
        assert in_callback("r1") is True
        assert in_callback("r2") is False
    finally:
        br.reset_allow(token)
    assert in_callback("r1") is False
    assert "r1" not in br._vouched


def test_nested_vouches_for_one_host_release_one_at_a_time(_locked):
    outer = br.allow_hosts(["devbox"], run_id="r1")
    inner = br.allow_hosts(["devbox"], run_id="r1")
    br.reset_allow(inner)
    assert br._vouched["r1"] == ["devbox"]
    br.reset_allow(outer)
    assert "r1" not in br._vouched
    assert br._request_ok("r1", "http://devbox/") is False


def test_blocked_connections_are_named_for_the_agent(_locked):
    br._console["g"] = []
    br._record_blocked("g", "https://evil.example:443/", "connection")
    assert br.drain_console("g") == [{
        "kind": "blocked", "level": "warning", "url": "https://evil.example:443/",
        "text": "connection blocked: host not local or allowlisted"}]


def test_a_late_block_after_destroy_does_not_revive_the_run(_locked):
    br._record_blocked("gone", "https://evil.example:443/", "connection")
    assert "gone" not in br._console


@pytest.mark.parametrize("host,port,ok", [
    ("fd12::5", 3000, True),          # ULA LAN dev server, bracketed correctly
    ("::1", 8080, True),
    ("cafe::1", 443, False),          # must not be read as host "cafe"
    ("evil.example", 443, False),
])
def test_proxy_checks_ipv6_hosts_whole(_locked, host, port, ok):
    br._vouched["r"] = ["cafe"]
    assert br._proxy_allows("r", host, port) is ok


def test_context_goes_through_its_own_egress_proxy(monkeypatch):
    ctx = MagicMock()
    browser = MagicMock()
    browser.new_context.return_value = ctx
    br._pw_handle = MagicMock()
    br._browser = browser
    br._get_context("px")
    try:
        port = br._proxies["px"].port
        assert port > 0
        px = br._proxies["px"]
        browser.new_context.assert_called_once_with(
            service_workers="block",
            proxy={"server": f"http://127.0.0.1:{port}",
                   "username": px.username, "password": px.password})
    finally:
        br._contexts["keep"] = (MagicMock(), MagicMock())
        br.destroy_context("px")
        br._contexts.pop("keep", None)
    assert "px" not in br._proxies


def test_a_failed_context_stops_its_proxy(monkeypatch):
    browser = MagicMock()
    browser.new_context.side_effect = RuntimeError("boom")
    br._pw_handle = MagicMock()
    br._browser = browser
    with pytest.raises(RuntimeError):
        br._get_context("bad")
    assert "bad" not in br._proxies


def test_launch_keeps_dns_and_webrtc_behind_the_proxy():
    args = " ".join(br._LAUNCH["args"])
    assert "disable_non_proxied_udp" in args
    assert "MAP * ~NOTFOUND" in args and "EXCLUDE localhost" in args
    assert br._LAUNCH["headless"] is True


def test_a_failed_goto_lets_the_error_page_settle_before_raising(monkeypatch):
    # Otherwise the late chrome-error commit interrupts the NEXT goto.
    monkeypatch.setattr(br, "_playwright_available", lambda: True)
    page = MagicMock()
    page.goto.side_effect = RuntimeError("net::ERR_BLOCKED_BY_CLIENT")
    monkeypatch.setattr(br, "_get_context", lambda rid: (MagicMock(), page))
    out = br.browse("goto", url="http://localhost:5173/go-out")
    assert out["error"] == "browser_op_failed"
    wait = page.wait_for_url.call_args
    assert wait.kwargs["timeout"] == 3000
    assert wait.args[0]("chrome-error://chromewebdata/") is True
    assert wait.args[0]("http://localhost:5173/") is False
