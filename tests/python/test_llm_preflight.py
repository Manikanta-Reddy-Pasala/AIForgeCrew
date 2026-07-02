"""Connect-preflight: an unreachable LLM endpoint fails fast (seconds) instead
of blocking the full request timeout on TCP connect — the simple-chat
equivalent of the pipeline connect-timeout fix."""
from __future__ import annotations

import socket
import time

import pytest

from aiforge_core.llm import client as c


def test_preflight_reachable_passes(monkeypatch):
    # A listening socket → preflight returns cleanly (no raise).
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        monkeypatch.setenv("AIFORGE_LLM_CONNECT_TIMEOUT_S", "3")
        c._preflight(f"http://127.0.0.1:{port}")   # no raise
    finally:
        srv.close()


def test_preflight_unreachable_fails_fast(monkeypatch):
    # TEST-NET-1 (192.0.2.0/24) is guaranteed non-routable → connect hangs →
    # bounded by the short connect timeout, NOT the 600s request timeout.
    monkeypatch.setenv("AIFORGE_LLM_CONNECT_TIMEOUT_S", "2")
    t0 = time.monotonic()
    with pytest.raises(ConnectionError):
        c._preflight("http://192.0.2.1:9")
    assert time.monotonic() - t0 < 6   # ~2s, well under any request timeout


def test_preflight_disabled_with_zero(monkeypatch):
    monkeypatch.setenv("AIFORGE_LLM_CONNECT_TIMEOUT_S", "0")
    c._preflight("http://192.0.2.1:9")   # disabled → no probe, no raise


def test_preflight_refused_port_raises(monkeypatch):
    monkeypatch.setenv("AIFORGE_LLM_CONNECT_TIMEOUT_S", "2")
    with pytest.raises(ConnectionError):
        c._preflight("http://127.0.0.1:1")   # nothing listening → refused
