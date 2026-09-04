"""Network+telemetry lockdown — arbitrary web egress is gated OFF by default.

There is no web SEARCH; the remaining egress is a page read of a URL someone
supplied. ``web_read`` is researcher-ONLY but no longer ungated — it answers to
the same switches as everything else (the researcher's role scoping is its own
flag) plus the LLM endpoint. ``fetch_url`` / ``http_get`` / ``web_fetch`` and
the headless browser must refuse arbitrary URLs unless the operator opts in via
``AIFORGE_ALLOW_WEB_FETCH=1``.
"""
from __future__ import annotations

import pathlib

import pytest

from aiforge_core.runtime import doer_tools
from aiforge_core.runtime.tools import browser as browser_tool


# ── fetch_url / http_get / web_fetch gated ──────────────────────────────

def test_fetch_url_disabled_by_default_makes_no_request(monkeypatch):
    monkeypatch.delenv("AIFORGE_ALLOW_WEB_FETCH", raising=False)

    def _boom(*a, **k):  # pragma: no cover - must never be reached
        raise AssertionError("network call made while web fetch disabled")

    monkeypatch.setattr(doer_tools.urllib.request, "urlopen", _boom)

    out = doer_tools.fetch_url("https://example.com")
    assert out["ok"] is False
    assert "web fetch disabled" in out["error"]
    assert "AIFORGE_ALLOW_WEB_FETCH=1" in out["error"]


def test_http_get_and_web_fetch_aliases_also_gated(monkeypatch):
    monkeypatch.delenv("AIFORGE_ALLOW_WEB_FETCH", raising=False)

    def _boom(*a, **k):  # pragma: no cover
        raise AssertionError("network call made while web fetch disabled")

    monkeypatch.setattr(doer_tools.urllib.request, "urlopen", _boom)

    for fn in (doer_tools.http_get, doer_tools.web_fetch):
        out = fn("https://example.com")
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
    out = doer_tools.fetch_url("https://example.com")
    assert out["ok"] is True
    assert out["body"] == "hello world"
    assert out["status"] == 200


def test_no_web_search_tool_exists():
    """Web SEARCH was removed (the query string was unfiltered outbound data).
    Nothing may reintroduce it as a doer tool or a module function."""
    import aiforge_core.runtime.tools.web_fetch as _wf

    assert not hasattr(doer_tools, "web_search")
    assert not hasattr(_wf, "web_search")
    assert "duckduckgo" not in pathlib.Path(_wf.__file__).read_text().lower()


# ── browser deny-by-default ─────────────────────────────────────────────

def test_browser_empty_allowlist_denies_the_open_web(monkeypatch):
    """CHANGED 2026-09-03: loopback is no longer denied here.

    The lockdown is about EGRESS, and an operator who locks the box down and
    clears the browser allowlist would otherwise lose ui_check against their
    own dev server — a control doing something nobody asked for. An external
    host is still denied without an allowlist or the fetch switch."""
    monkeypatch.delenv("AIFORGE_BROWSER_ALLOWLIST", raising=False)
    monkeypatch.delenv("AIFORGE_ALLOW_WEB_FETCH", raising=False)
    assert browser_tool._allowlist_ok("https://example.com") is False
    assert browser_tool._allowlist_ok("http://127.0.0.1:8799") is True


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
    """The gate must return BEFORE reading the ticket's external refs.

    This used to prove it by patching
    `aiforge_memory.features.external_ingest.ingest_external_source` with a
    boom. That module went with the Neo4j layer, and a STRING target makes
    monkeypatch import it — `raising=False` forgives a missing attribute, not
    a missing module, so the patch itself raised ImportError. The gate is now
    checked at the seam that still exists: with the gate off, nothing may even
    look at the refs.
    """
    from aiforge_core.runtime import adk_runner
    from aiforge_core.runtime.adk_runner import _orchestrate

    monkeypatch.setenv("AIFORGE_EXTERNAL_INGEST", "0")

    class _Ticket:
        project = "demo"
        metadata = {"external_refs": ["http://example.com/spec"]}

    def _boom(*a, **k):  # pragma: no cover
        raise AssertionError("external refs read while the gate is off")

    monkeypatch.setattr(_orchestrate, "_external_refs", _boom)
    adk_runner._ingest_ticket_external_refs(_Ticket())   # returns quietly


def test_external_ingest_reads_refs_when_on(monkeypatch):
    """The counterpart, so the test above cannot pass by the gate being stuck
    on 'off' — with the flag enabled the refs ARE read."""
    from aiforge_core.runtime import adk_runner
    from aiforge_core.runtime.adk_runner import _orchestrate

    monkeypatch.setenv("AIFORGE_EXTERNAL_INGEST", "1")
    seen = []

    class _Ticket:
        project = "demo"
        metadata = {"external_refs": ["http://example.com/spec"]}

    monkeypatch.setattr(_orchestrate, "_external_refs",
                        lambda t: seen.append(t) or [])
    adk_runner._ingest_ticket_external_refs(_Ticket())
    assert seen, "with the gate ON the refs must be read"


def test_external_ingest_gate_present_in_source():
    import inspect

    from aiforge_core.runtime import adk_runner

    # adk_runner was split into a package; read the source of the gated
    # function directly rather than the package __init__ file.
    text = inspect.getsource(adk_runner._ingest_ticket_external_refs)
    assert 'os.environ.get("AIFORGE_EXTERNAL_INGEST"' in text
