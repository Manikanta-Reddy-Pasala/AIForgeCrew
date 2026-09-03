from __future__ import annotations

import urllib.error

import pytest

from aiforge_core.runtime.visual import _ask, _captures, _macro


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))


@pytest.fixture
def capture():
    return _captures.save_capture(b"\x89PNG\r\n\x1a\n", "ui")[0]


def test_answers_a_question(monkeypatch, capture):
    monkeypatch.setattr(_ask, "ask_image",
                        lambda p, q, role="chat": {"ok": True, "text": f"A:{q}",
                                                   "vision_role": "vision"})
    out = _ask.ui_ask({"capture_id": capture, "question": "overlap?"})
    assert out["ok"] is True
    assert out["answer"] == "A:overlap?"
    assert out["capture_id"] == capture


def test_no_question_falls_back_to_the_audit(monkeypatch, capture):
    monkeypatch.setattr(_ask, "audit_image",
                        lambda p, role="chat": {"ok": True, "text": "SCREEN: x"})
    out = _ask.ui_ask({"capture_id": capture})
    assert out["answer"] == "SCREEN: x"


def test_missing_capture_id():
    assert _ask.ui_ask({})["error"] == "missing_capture_id"


def test_unknown_capture():
    out = _ask.ui_ask({"capture_id": "ui-1-deadbeef", "question": "?"})
    assert out["error"] == "capture_not_found"


def test_traversal_capture_id_is_not_readable():
    out = _ask.ui_ask({"capture_id": "../../../etc/passwd", "question": "?"})
    assert out["error"] == "capture_not_found"


def test_vision_failure_surfaces_the_hint(monkeypatch, capture):
    monkeypatch.setattr(_ask, "ask_image",
                        lambda p, q, role="chat": {"ok": False,
                                                   "error": "no_vision_model",
                                                   "hint": "wire a VLM"})
    out = _ask.ui_ask({"capture_id": capture, "question": "?"})
    assert out["ok"] is False
    assert out["hint"] == "wire a VLM"


# ── readiness polling ────────────────────────────────────────────────────────

def test_http_error_counts_as_ready(monkeypatch):
    # A LOCAL target: the readiness probe used to GET whatever it was handed,
    # ungated, in a retry loop — so ui_check with an external URL was a live
    # outbound path. It now refuses a non-local target, and ui_check is for a
    # dev server on this machine anyway.
    def _raise(*a, **kw):
        raise urllib.error.HTTPError("http://localhost:5173", 500, "boom", {},
                                     None)

    monkeypatch.setattr("urllib.request.urlopen", _raise)
    ready, _ = _macro._wait_ready("http://localhost:5173", 5)
    # A 500 IS the page worth screenshotting — often it is the bug itself.
    assert ready is True


def test_connection_refused_times_out(monkeypatch):
    def _raise(*a, **kw):
        raise OSError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", _raise)
    monkeypatch.setattr("time.sleep", lambda s: None)
    ready, why = _macro._wait_ready("http://localhost:5173", 0)
    assert ready is False
    assert "refused" in why
