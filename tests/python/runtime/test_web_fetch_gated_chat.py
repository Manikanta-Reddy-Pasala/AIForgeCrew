"""web_fetch (arbitrary URL) must obey the AIFORGE_ALLOW_WEB_FETCH lockdown —
the chat agent's web_fetch routes through this. web_SEARCH stays allowed."""
from __future__ import annotations
from aiforge_core.runtime.tools import web_search as ws


def test_web_fetch_gated_by_default(monkeypatch):
    monkeypatch.delenv("AIFORGE_ALLOW_WEB_FETCH", raising=False)
    called = {"n": 0}
    monkeypatch.setattr(ws, "_get", lambda *a, **k: called.__setitem__("n", 1) or "<html></html>")
    r = ws.web_fetch({"url": "http://example.com"})
    assert r["ok"] is False and "disabled" in r["error"]
    assert called["n"] == 0   # no outbound attempt


def test_web_fetch_allowed_when_opted_in(monkeypatch):
    monkeypatch.setenv("AIFORGE_ALLOW_WEB_FETCH", "1")
    monkeypatch.setattr(ws, "_get", lambda *a, **k: "<title>t</title><body>hi</body>")
    r = ws.web_fetch({"url": "http://example.com"})
    assert r["ok"] is True


def test_chat_web_fetch_tool_gated(monkeypatch):
    # the chat TOOLS['web_fetch'] must be blocked too (it routes here)
    monkeypatch.delenv("AIFORGE_ALLOW_WEB_FETCH", raising=False)
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("OUTBOUND")))
    from aiforge_core.runtime import chat_agent as ca
    r = ca.TOOLS["web_fetch"]({"url": "http://example.com"}, "/tmp")
    assert r["ok"] is False
