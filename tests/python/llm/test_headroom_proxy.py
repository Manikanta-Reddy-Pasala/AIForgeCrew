"""aiforge_core.llm.headroom.proxy_base — the transparent forwarding-proxy
redirect that gives every agent compression from one place. Verifies: no-op
when off, redirects ONLY the fronted upstream (host:port match), preserves the
path, and leaves cloud / other-host endpoints alone."""
from __future__ import annotations

import pytest

from aiforge_core.llm import headroom as hr


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("AIFORGE_HEADROOM", "AIFORGE_HEADROOM_URL", "AIFORGE_HEADROOM_UPSTREAM"):
        monkeypatch.delenv(k, raising=False)
    yield


def test_disabled_is_passthrough():
    assert hr.proxy_base("http://127.0.0.1:1234/v1") == "http://127.0.0.1:1234/v1"


def test_none_and_empty_passthrough(monkeypatch):
    monkeypatch.setenv("AIFORGE_HEADROOM", "1")
    assert hr.proxy_base(None) is None
    assert hr.proxy_base("") == ""


def test_redirects_fronted_endpoint_preserving_path(monkeypatch):
    monkeypatch.setenv("AIFORGE_HEADROOM", "1")
    # Default upstream 127.0.0.1:1234, proxy 127.0.0.1:8787.
    assert hr.proxy_base("http://127.0.0.1:1234/v1") == "http://127.0.0.1:8787/v1"
    # No /v1 suffix — still swaps host:port only.
    assert hr.proxy_base("http://127.0.0.1:1234") == "http://127.0.0.1:8787"


def test_leaves_other_host_untouched(monkeypatch):
    monkeypatch.setenv("AIFORGE_HEADROOM", "1")
    # A cloud endpoint or a different local box must NOT be redirected.
    assert hr.proxy_base("https://api.anthropic.com") == "https://api.anthropic.com"
    assert hr.proxy_base("http://192.168.1.50:1234/v1") == "http://192.168.1.50:1234/v1"


def test_different_port_not_matched(monkeypatch):
    monkeypatch.setenv("AIFORGE_HEADROOM", "1")
    # Same host, different port than the upstream → not the fronted endpoint.
    assert hr.proxy_base("http://127.0.0.1:5678/v1") == "http://127.0.0.1:5678/v1"


def test_custom_upstream_and_proxy(monkeypatch):
    monkeypatch.setenv("AIFORGE_HEADROOM", "1")
    monkeypatch.setenv("AIFORGE_HEADROOM_UPSTREAM", "http://gpu-box:1234")
    monkeypatch.setenv("AIFORGE_HEADROOM_URL", "http://gpu-box:9999")
    assert hr.proxy_base("http://gpu-box:1234/v1") == "http://gpu-box:9999/v1"
    # The old default upstream is now NOT the fronted one → untouched.
    assert hr.proxy_base("http://127.0.0.1:1234/v1") == "http://127.0.0.1:1234/v1"


def test_upstream_with_v1_matches_base_without(monkeypatch):
    monkeypatch.setenv("AIFORGE_HEADROOM", "1")
    monkeypatch.setenv("AIFORGE_HEADROOM_UPSTREAM", "http://127.0.0.1:1234/v1")
    # Comparison is host:port only, so a /v1 on the upstream still matches.
    assert hr.proxy_base("http://127.0.0.1:1234/v1") == "http://127.0.0.1:8787/v1"


def test_garbage_url_soft_passthrough(monkeypatch):
    monkeypatch.setenv("AIFORGE_HEADROOM", "1")
    assert hr.proxy_base("not a url") == "not a url"
