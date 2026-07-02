"""Network+telemetry lockdown — arbitrary web egress is gated OFF by default.

The ONLY sanctioned agent egress is the researcher's ``web_search`` (its own
flag) plus the LLM endpoint. ``fetch_url`` / ``http_get`` / ``web_fetch`` and
the headless browser must refuse arbitrary URLs unless the operator opts in via
``AIFORGE_ALLOW_WEB_FETCH=1``.
"""
from __future__ import annotations

import pytest

from aiforge_core.runtime import doer_tools
from aiforge_core.runtime.tools import browser as browser_tool


# ── fetch_url / http_get / web_fetch gated ──────────────────────────────

def test_fetch_url_disabled_by_default_makes_no_request(monkeypatch):
    monkeypatch.delenv("AIFORGE_ALLOW_WEB_FETCH", raising=False)

    def _boom(*a, **k):  # pragma: no cover - must never be reached
        raise AssertionError("network call made while web fetch disabled")

    monkeypatch.setattr(doer_tools.urllib.request, "urlopen", _boom)

    out = doer_tools.fetch_url("http://example.com")
    assert out["ok"] is False
    assert "web fetch disabled" in out["error"]
    assert "AIFORGE_ALLOW_WEB_FETCH=1" in out["error"]


def test_http_get_and_web_fetch_aliases_also_gated(monkeypatch):
    monkeypatch.delenv("AIFORGE_ALLOW_WEB_FETCH", raising=False)

    def _boom(*a, **k):  # pragma: no cover
        raise AssertionError("network call made while web fetch disabled")

    monkeypatch.setattr(doer_tools.urllib.request, "urlopen", _boom)

    for fn in (doer_tools.http_get, doer_tools.web_fetch):
        out = fn("http://example.com")
        assert out["ok"] is False
        assert "web fetch disabled" in out["error"]


def test_fetch_url_proceeds_when_opted_in(monkeypatch):
    monkeypatch.setenv("AIFORGE_ALLOW_WEB_FETCH", "1")

    class _Resp:
        status = 200
        headers = {"Content-Type": "text/plain"}

        def read(self, n):
            return b"hello world"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(
        doer_tools.urllib.request, "urlopen", lambda *a, **k: _Resp()
    )
    out = doer_tools.fetch_url("http://example.com")
    assert out["ok"] is True
    assert out["body"] == "hello world"
    assert out["status"] == 200


def test_web_search_unaffected_by_fetch_flag(monkeypatch):
    """web_search is the researcher's allowed egress — the fetch gate must
    not touch it. It routes through its own tool module (stubbed here)."""
    monkeypatch.delenv("AIFORGE_ALLOW_WEB_FETCH", raising=False)
    import aiforge_core.runtime.tools.web_search as _ws

    called = {}

    def _stub(payload):
        called["q"] = payload.get("query")
        return {"ok": True, "results": [{"title": "t", "url": "u", "snippet": "s"}]}

    monkeypatch.setattr(_ws, "web_search", _stub)
    out = doer_tools.web_search("how to foo", k=3)
    assert out["ok"] is True
    assert called["q"] == "how to foo"


# ── browser deny-by-default ─────────────────────────────────────────────

def test_browser_empty_allowlist_denies(monkeypatch):
    monkeypatch.delenv("AIFORGE_BROWSER_ALLOWLIST", raising=False)
    monkeypatch.delenv("AIFORGE_ALLOW_WEB_FETCH", raising=False)
    assert browser_tool._allowlist_ok("http://example.com") is False
    assert browser_tool._allowlist_ok("http://127.0.0.1:8799") is False


def test_browser_explicit_allowlist(monkeypatch):
    monkeypatch.setenv("AIFORGE_BROWSER_ALLOWLIST", "127.0.0.1,localhost")
    monkeypatch.delenv("AIFORGE_ALLOW_WEB_FETCH", raising=False)
    assert browser_tool._allowlist_ok("http://localhost:3000/") is True
    assert browser_tool._allowlist_ok("http://127.0.0.1:8799") is True
    assert browser_tool._allowlist_ok("https://example.com") is False


def test_browser_empty_allowlist_allows_when_opted_in(monkeypatch):
    monkeypatch.delenv("AIFORGE_BROWSER_ALLOWLIST", raising=False)
    monkeypatch.setenv("AIFORGE_ALLOW_WEB_FETCH", "1")
    assert browser_tool._allowlist_ok("https://example.com") is True


# ── external-ingest gate (#8) — existing gate must skip when off ─────────

def test_external_ingest_skipped_when_off(monkeypatch):
    from aiforge_core.runtime import adk_runner

    monkeypatch.setenv("AIFORGE_EXTERNAL_INGEST", "0")

    class _Ticket:
        project = "demo"
        metadata = {"external_refs": ["http://example.com/spec"]}

    def _boom(*a, **k):  # pragma: no cover
        raise AssertionError("external_ingest driver invoked while gated off")

    # If the gate is honoured, we return before importing/using the driver.
    monkeypatch.setattr(
        "aiforge_memory.features.external_ingest.ingest_external_source",
        _boom,
        raising=False,
    )
    # Should return quietly, invoking nothing.
    adk_runner._ingest_ticket_external_refs(_Ticket())


def test_external_ingest_gate_present_in_source():
    from aiforge_core.runtime import adk_runner

    with open(adk_runner.__file__, encoding="utf-8") as fh:
        text = fh.read()
    assert 'os.environ.get("AIFORGE_EXTERNAL_INGEST"' in text
