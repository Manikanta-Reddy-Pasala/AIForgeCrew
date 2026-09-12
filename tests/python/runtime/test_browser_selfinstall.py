"""A missing Chromium build is something the sandbox installs, not a dead end.

The image ships the playwright PACKAGE but not its ~150MB browser download, so
``_playwright_available()`` said yes while ``chromium.launch()`` died on
"Executable doesn't exist" — and ui_check / browse reported
``browser_launch_failed`` with no way forward. Inside the box the agent is
expected to install what the task needs; a browser is no different.
"""
from __future__ import annotations

import pytest

from aiforge_core.runtime.tools import browser


class _FakePW:
    """Chromium that fails until the build is 'installed', then works."""

    def __init__(self, fail_times: int, message: str):
        self.calls = 0
        self.fail_times = fail_times
        self.message = message
        self.chromium = self

    def launch(self, **_kw):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError(self.message)
        return "browser"


_MISSING = ("BrowserType.launch: Executable doesn't exist at "
            "/root/.cache/ms-playwright/chromium-1234/chrome-linux/chrome\n"
            "Please run the following command to download new browsers:\n"
            "playwright install")


def test_a_missing_browser_build_is_installed_once_then_retried(monkeypatch):
    installed: list[bool] = []
    monkeypatch.setattr(browser, "_install_chromium",
                        lambda: installed.append(True) or "")
    pw = _FakePW(fail_times=1, message=_MISSING)
    assert browser._launch_chromium(pw) == "browser"
    assert installed == [True]
    assert pw.calls == 2                      # one failure, one retry


def test_an_install_failure_says_so_instead_of_looping(monkeypatch):
    monkeypatch.setattr(browser, "_install_chromium", lambda: "no disk space")
    pw = _FakePW(fail_times=2, message=_MISSING)
    with pytest.raises(RuntimeError, match="chromium install failed: no disk space"):
        browser._launch_chromium(pw)
    assert pw.calls == 1                      # never retried after a failed install


def test_an_unrelated_launch_error_is_not_an_install_problem(monkeypatch):
    """Only the missing-build wording triggers a download; everything else
    propagates untouched, so a real bug is not masked by a 150MB retry."""
    monkeypatch.setattr(browser, "_install_chromium",
                        lambda: pytest.fail("must not install"))
    pw = _FakePW(fail_times=1, message="Target page, context or browser has been closed")
    with pytest.raises(RuntimeError, match="has been closed"):
        browser._launch_chromium(pw)


def test_the_install_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("AIFORGE_ALLOW_INSTALL", "0")
    monkeypatch.setattr(browser, "_install_chromium",
                        lambda: pytest.fail("must not install"))
    pw = _FakePW(fail_times=1, message=_MISSING)
    with pytest.raises(RuntimeError, match="Executable doesn't exist"):
        browser._launch_chromium(pw)


def test_the_agent_can_also_ask_for_a_browser_and_tmux_by_name():
    """ensure_runtime is the explicit route — "install chromium" as a request,
    rather than waiting for a launch to fail."""
    from aiforge_core.runtime.tools import ensure_runtime
    for tool in ("tmux", "chromium", "xvfb"):
        assert tool in ensure_runtime._APT, tool
