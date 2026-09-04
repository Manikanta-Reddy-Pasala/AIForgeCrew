from __future__ import annotations

import pytest

from aiforge_core.runtime.visual import _macro


class _FakeBrowser:
    """Records the command sequence ui_check drives."""

    def __init__(self, png=b"PNG", console=None, goto_ok=True):
        self.calls: list[tuple] = []
        self.run_ids: list = []          # every context the macro touched
        self._png = png
        self._console = console or []
        self._goto_ok = goto_ok

    def browse(self, command, **kw):
        self.calls.append((command, kw))
        self.run_ids.append(kw.get("_run_id"))
        if command == "goto" and not self._goto_ok:
            return {"ok": False, "error": "browser_op_failed"}
        return {"ok": True}

    def screenshot_bytes(self, **kw):
        self.calls.append(("screenshot_bytes", kw))
        self.run_ids.append(kw.get("run_id"))
        return self._png, None

    def drain_console(self, run_id=None, **kw):
        self.run_ids.append(run_id)
        return list(self._console)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(_macro, "_wait_ready", lambda url, t: (True, ""))


def _install(monkeypatch, fake, audit=None):
    import aiforge_core.runtime.tools.browser as real
    for name in ("browse", "screenshot_bytes", "drain_console"):
        monkeypatch.setattr(real, name, getattr(fake, name))
    monkeypatch.setattr(
        _macro, "audit_image",
        lambda path, role="chat": audit if audit is not None else
        {"ok": True, "text": "SCREEN: ok\nISSUES:\n- none", "vision_role": "vision"})


def test_url_path_capture_and_audit(monkeypatch):
    fake = _FakeBrowser()
    _install(monkeypatch, fake)
    out = _macro.ui_check({"url": "http://localhost:5173", "path": "/login"})
    assert out["ok"] is True
    assert out["url"] == "http://localhost:5173/login"
    assert out["capture_id"]
    assert "ISSUES" in out["audit"]
    # No base64 anywhere: the whole point is that the model gets words.
    assert "png_b64" not in out


def test_sizes_the_viewport_before_navigating(monkeypatch):
    fake = _FakeBrowser()
    _install(monkeypatch, fake)
    _macro.ui_check({"url": "http://x:1/", "width": 390, "height": 844})
    order = [c[0] for c in fake.calls]
    assert order.index("viewport") < order.index("goto")
    assert fake.calls[0][1]["width"] == 390
    assert fake.calls[0][1]["height"] == 844


def test_settles_before_capturing(monkeypatch):
    fake = _FakeBrowser()
    _install(monkeypatch, fake)
    _macro.ui_check({"url": "http://x:1/"})
    order = [c[0] for c in fake.calls]
    assert order.index("wait_for") < order.index("screenshot_bytes")


def test_every_step_uses_the_callers_browser_context(monkeypatch):
    fake = _FakeBrowser()
    _install(monkeypatch, fake)
    _macro.ui_check({"url": "http://x:1/"}, None, run_id="chat-abc")
    # A different context per step means a follow-up browse click lands on a
    # fresh about:blank, and `browse console` reads an empty buffer.
    assert set(fake.run_ids) == {"chat-abc"}


def test_console_entries_are_capped_for_the_observation(monkeypatch):
    console = [{"kind": "pageerror", "level": "error", "text": "x" * 900}
               for _ in range(40)]
    fake = _FakeBrowser(console=console)
    _install(monkeypatch, fake)
    out = _macro.ui_check({"url": "http://x:1/"})
    assert out["console_error_count"] == 40          # the true count survives
    assert len(out["console_errors"]) == _macro._MAX_CONSOLE_REPORTED
    assert out["console_errors_omitted"] == 40 - _macro._MAX_CONSOLE_REPORTED
    assert all(len(e["text"]) <= _macro._MAX_CONSOLE_TEXT + 1
               for e in out["console_errors"])


def test_audit_comes_first_in_the_result(monkeypatch):
    fake = _FakeBrowser(console=[{"kind": "pageerror", "level": "error",
                                  "text": "y" * 400} for _ in range(8)])
    _install(monkeypatch, fake)
    out = _macro.ui_check({"url": "http://x:1/"})
    # The observation is capped and serialized in insertion order: an audit
    # emitted last is the first thing a noisy page pushes out.
    assert list(out)[:2] == ["ok", "audit"]


def test_console_errors_are_reported(monkeypatch):
    console = [{"kind": "pageerror", "level": "error", "text": "boom"},
               {"kind": "console", "level": "log", "text": "hello"}]
    fake = _FakeBrowser(console=console)
    _install(monkeypatch, fake)
    out = _macro.ui_check({"url": "http://x:1/"})
    assert out["console_error_count"] == 1
    assert out["console_errors"][0]["text"] == "boom"


def test_missing_vision_still_returns_the_capture(monkeypatch):
    fake = _FakeBrowser()
    _install(monkeypatch, fake,
             audit={"ok": False, "error": "no_vision_model", "hint": "set it"})
    out = _macro.ui_check({"url": "http://x:1/"})
    # A missing VLM downgrades the answer; it must not fail the check, and it
    # must never read as "the screen is fine".
    assert out["ok"] is True
    assert out["audit"] == ""
    assert out["audit_error"] == "no_vision_model"
    assert "set it" in out["audit_hint"]
    assert "NOTHING READ THIS SCREEN" in out["audit_note"]


def test_a_working_vlm_is_never_reported_as_a_missing_one(monkeypatch):
    fake = _FakeBrowser()
    # The VLM is configured and reachable; THIS capture was unreadable.
    _install(monkeypatch, fake, audit={"ok": False, "error": "image_too_large"})
    out = _macro.ui_check({"url": "http://x:1/", "full_page": True})
    assert out["audit_error"] == "image_too_large"
    assert "no vision model" not in out["audit_hint"]
    assert "image_too_large" in out["audit_hint"]


def test_reuses_a_running_service(monkeypatch):
    fake = _FakeBrowser()
    _install(monkeypatch, fake)
    import aiforge_core.runtime.tools.serve as serve
    monkeypatch.setattr(serve, "list_services", lambda *a, **kw: {
        "ok": True, "services": [{"pid": 1, "url": "http://localhost:3000",
                                  "cmd": "npm run dev", "alive": True}]})
    started = []
    monkeypatch.setattr(serve, "serve",
                        lambda *a, **kw: started.append(a) or {"ok": True})
    out = _macro.ui_check({"cmd": "npm run dev"})
    assert out["url"] == "http://localhost:3000"
    assert started == []          # a second dev server would bind a second port
    # A reused server is named too, so the agent can see WHICH one it looked at.
    assert out["service"] == {"pid": 1, "url": "http://localhost:3000",
                              "cmd": "npm run dev", "started": False}


def test_starts_the_service_when_none_is_running(monkeypatch):
    fake = _FakeBrowser()
    _install(monkeypatch, fake)
    import aiforge_core.runtime.tools.serve as serve
    monkeypatch.setattr(serve, "list_services",
                        lambda *a, **kw: {"ok": True, "services": []})
    monkeypatch.setattr(serve, "serve", lambda args, cwd=None: {
        "ok": True, "pid": 42, "url": "http://localhost:5173"})
    out = _macro.ui_check({"cmd": "npm run dev", "path": "/x"})
    assert out["url"] == "http://localhost:5173/x"
    assert out["service"] == {"pid": 42, "url": "http://localhost:5173",
                              "cmd": "npm run dev", "started": True}


def test_serve_failure_is_returned(monkeypatch):
    import aiforge_core.runtime.tools.serve as serve
    monkeypatch.setattr(serve, "list_services",
                        lambda *a, **kw: {"ok": True, "services": []})
    monkeypatch.setattr(serve, "serve", lambda args, cwd=None: {
        "ok": False, "error": "service exited on startup (code 1)"})
    out = _macro.ui_check({"cmd": "npm run dev"})
    assert out["ok"] is False
    assert "exited" in out["error"]


def test_needs_a_url_or_cmd(monkeypatch):
    import aiforge_core.runtime.tools.serve as serve
    monkeypatch.setattr(serve, "list_services",
                        lambda *a, **kw: {"ok": True, "services": []})
    out = _macro.ui_check({})
    assert out["error"] == "missing_url_or_cmd"


def test_server_never_answers(monkeypatch):
    monkeypatch.setattr(_macro, "_wait_ready",
                        lambda url, t: (False, "connection refused"))
    out = _macro.ui_check({"url": "http://localhost:9/"})
    assert out["error"] == "server_not_reachable"
    assert "connection refused" in out["detail"]


def test_navigation_failure_is_soft(monkeypatch):
    fake = _FakeBrowser(goto_ok=False)
    _install(monkeypatch, fake)
    out = _macro.ui_check({"url": "http://x:1/"})
    assert out["ok"] is False
    assert out["error"] == "navigation_failed"


def test_dev_server_host_is_vouched_for_only_during_the_call(monkeypatch):
    monkeypatch.setenv("AIFORGE_BROWSER_ALLOWLIST", "github.com")
    import aiforge_core.runtime.tools.browser as real
    seen = {}
    fake = _FakeBrowser()
    _install(monkeypatch, fake)

    dev = "http://localhost:5173/"

    def _shot(**kw):
        # Mid-call the dev server is reachable despite the allowlist…
        seen["during"] = real._allowlist_ok(dev)
        seen["vouched"] = real._EXTRA_ALLOW.get()
        seen["other"] = real._allowlist_ok("http://169.254.169.254/")
        return b"PNG", None

    monkeypatch.setattr(real, "screenshot_bytes", _shot)
    _macro.ui_check({"url": dev})
    assert seen["during"] is True
    # …a host nobody vouched for is still refused, mid-call…
    assert seen["other"] is False
    # …the vouch existed during the call…
    assert "localhost" in seen["vouched"]
    # …and the GRANT does not outlive it. (Asserted on the vouch set itself,
    # not via _allowlist_ok: loopback is now allowed unconditionally — it is a
    # dev server, not egress — so that check could no longer tell a live grant
    # from an expired one.)
    assert real._EXTRA_ALLOW.get() == ()
    import os
    assert os.environ["AIFORGE_BROWSER_ALLOWLIST"] == "github.com"


def test_a_model_chosen_public_host_is_not_vouched_for(monkeypatch):
    import aiforge_core.runtime.tools.serve as serve
    monkeypatch.setattr(serve, "list_services",
                        lambda *a, **kw: {"ok": True, "services": []})
    # Not loopback and not a served dev server: the existing allowlist and SSRF
    # guard must remain the only thing deciding.
    assert _macro._vouched_hosts("http://169.254.169.254/latest/meta-data",
                                 None) == frozenset()
    assert _macro._vouched_hosts("http://evil.example.com/", None) == frozenset()


def test_a_started_service_host_is_vouched_for():
    hosts = _macro._vouched_hosts("http://dev-box:5173/app",
                                  {"url": "http://dev-box:5173"})
    assert hosts == frozenset({"dev-box"})


@pytest.mark.parametrize("base,path,expected", [
    ("http://x:1", "/a", "http://x:1/a"),
    ("http://x:1/", "a", "http://x:1/a"),
    ("http://x:1/app", "b", "http://x:1/app/b"),
    ("http://x:1", "", "http://x:1"),
])
def test_path_join(base, path, expected):
    assert _macro._join(base, path) == expected


# ── url + cmd together (the shape an agent actually sends) ──────────────────

def test_url_plus_cmd_starts_the_server_when_the_url_is_dead(monkeypatch):
    fake = _FakeBrowser()
    _install(monkeypatch, fake)
    import aiforge_core.runtime.tools.serve as serve
    monkeypatch.setattr(serve, "list_services",
                        lambda *a, **kw: {"ok": True, "services": []})
    monkeypatch.setattr(serve, "serve", lambda args, cwd=None: {
        "ok": True, "pid": 7, "url": "http://localhost:8788"})
    # url is given but nothing answers there yet — the cmd is what fixes that.
    probes = iter([(False, "refused"), (True, "")])
    monkeypatch.setattr(_macro, "_wait_ready", lambda url, t: next(probes))
    out = _macro.ui_check({"url": "http://127.0.0.1:8788/", "path": "a.html",
                           "cmd": "python3 -m http.server 8788"})
    assert out["ok"] is True
    assert out["url"] == "http://127.0.0.1:8788/a.html"   # the caller's url wins
    assert out["service"]["started"] is True


def test_url_plus_cmd_does_not_start_a_second_server(monkeypatch):
    fake = _FakeBrowser()
    _install(monkeypatch, fake)
    import aiforge_core.runtime.tools.serve as serve
    monkeypatch.setattr(serve, "serve", lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("already reachable — must not start another")))
    monkeypatch.setattr(_macro, "_wait_ready", lambda url, t: (True, ""))
    out = _macro.ui_check({"url": "http://127.0.0.1:8788/",
                           "cmd": "python3 -m http.server 8788"})
    assert out["ok"] is True
