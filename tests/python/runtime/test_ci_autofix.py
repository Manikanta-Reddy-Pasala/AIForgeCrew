"""Unit tests for CI closed-loop autofix (gap A3).

``build_fix_request`` shapes a follow-up fix request from red checks;
``on_ci_red`` dispatches it only when the autofix flag is set and there
are failed checks. No real ``gh`` — dispatch is injected.
"""
from __future__ import annotations

import pytest

from aiforge_core.runtime import ci_feedback


PR = "https://github.com/acme/widgets/pull/42"
REPO = "acme/widgets"


def _failed(name, summary=""):
    return {"name": name, "conclusion": "failure", "summary": summary}


def test_build_fix_request_shape():
    checks = [_failed("build", "compile error"), _failed("test", "1 failing")]
    req = ci_feedback.build_fix_request(PR, REPO, checks)
    assert req["kind"] == "ci_fix"
    assert req["pr"] == PR
    assert req["repo"] == REPO
    assert req["checks"] == ["build", "test"]
    assert isinstance(req["title"], str) and req["title"]
    assert isinstance(req["body"], str) and req["body"]
    # body mentions failing check names + log excerpts
    assert "build" in req["body"]
    assert "compile error" in req["body"]


def test_build_fix_request_truncates_log_excerpt():
    huge = "x" * 10000
    req = ci_feedback.build_fix_request(PR, REPO, [_failed("build", huge)])
    # body capped to roughly ~2KB of excerpt (plus small header/labels)
    assert len(req["body"]) < 4000


def test_on_ci_red_dispatches_when_flag_set(monkeypatch):
    monkeypatch.setenv("AIFORGE_CI_AUTOFIX_ENABLED", "1")
    graded = {
        "ok": True,
        "status": "red",
        "pr_url": PR,
        "checks": [
            {"name": "build", "conclusion": "failure", "summary": "boom"},
            {"name": "lint", "conclusion": "success", "summary": ""},
        ],
    }
    seen = []

    def fake_dispatch(req):
        seen.append(req)
        return {"dispatched": True}

    result = ci_feedback.on_ci_red(graded, dispatch=fake_dispatch)
    assert len(seen) == 1
    req = seen[0]
    assert req["kind"] == "ci_fix"
    assert req["checks"] == ["build"]  # only the failing one
    assert result["checks"] == ["build"]


def test_on_ci_red_noop_when_flag_unset(monkeypatch):
    monkeypatch.delenv("AIFORGE_CI_AUTOFIX_ENABLED", raising=False)
    graded = {
        "ok": True,
        "status": "red",
        "pr_url": PR,
        "checks": [{"name": "build", "conclusion": "failure", "summary": "x"}],
    }
    seen = []

    def fake_dispatch(req):
        seen.append(req)

    result = ci_feedback.on_ci_red(graded, dispatch=fake_dispatch)
    assert seen == []  # never dispatched
    # still returns the fix request for inspection
    assert result["kind"] == "ci_fix"


def test_on_ci_red_no_dispatch_when_no_failed_checks(monkeypatch):
    monkeypatch.setenv("AIFORGE_CI_AUTOFIX_ENABLED", "1")
    graded = {
        "ok": True,
        "status": "green",
        "pr_url": PR,
        "checks": [{"name": "build", "conclusion": "success", "summary": ""}],
    }
    seen = []

    def fake_dispatch(req):
        seen.append(req)

    result = ci_feedback.on_ci_red(graded, dispatch=fake_dispatch)
    assert seen == []
    assert result is None


def test_on_ci_red_dispatch_none_no_side_effects(monkeypatch):
    monkeypatch.setenv("AIFORGE_CI_AUTOFIX_ENABLED", "1")
    graded = {
        "ok": True,
        "status": "red",
        "pr_url": PR,
        "checks": [{"name": "build", "conclusion": "failure", "summary": "x"}],
    }
    result = ci_feedback.on_ci_red(graded, dispatch=None)
    assert result["kind"] == "ci_fix"
    assert result["checks"] == ["build"]
