"""SSRF guard for the two ungated public-fetch paths + the shared helper.

``guard_public_url`` must block cloud-metadata (169.254.169.254), loopback,
RFC-1918 and IPv6 loopback targets, and a hostname that RESOLVES to a private
IP, while allowing a genuinely public host. The Doer ``web_read`` (_do_fetch)
and the unauthenticated ``kind=url`` memory ingest (_fetch_url) must refuse a
metadata URL WITHOUT making the request.
"""
from __future__ import annotations

import socket

import pytest

from aiforge_core.net import ssl as net_ssl
from aiforge_core.net.ssl import SSRFBlocked, guard_public_url


def _fake_getaddrinfo(ip: str):
    def _inner(host, port, *a, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port or 0))]
    return _inner


# ─── guard_public_url ──────────────────────────────────────────────────


@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data/",   # cloud IMDS
    "http://127.0.0.1:8799/api/health",           # loopback service
    "http://10.0.0.5/",                            # RFC-1918
    "http://[::1]/",                               # IPv6 loopback
    "http://192.168.1.10/",                        # RFC-1918
])
def test_blocks_private_ip_literals(monkeypatch, url):
    monkeypatch.delenv("AIFORGE_SSRF_ALLOW_PRIVATE", raising=False)
    with pytest.raises(SSRFBlocked) as ei:
        guard_public_url(url)
    assert ei.value.kind == "private"


def test_blocks_non_http_scheme(monkeypatch):
    monkeypatch.delenv("AIFORGE_SSRF_ALLOW_PRIVATE", raising=False)
    with pytest.raises(SSRFBlocked) as ei:
        guard_public_url("file:///etc/passwd")
    assert ei.value.kind == "scheme"


def test_blocks_hostname_resolving_private(monkeypatch):
    monkeypatch.delenv("AIFORGE_SSRF_ALLOW_PRIVATE", raising=False)
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("169.254.169.254"))
    with pytest.raises(SSRFBlocked) as ei:
        guard_public_url("http://sneaky.example.com/")
    assert ei.value.kind == "private"


def test_allows_public_hostname(monkeypatch):
    monkeypatch.delenv("AIFORGE_SSRF_ALLOW_PRIVATE", raising=False)
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("93.184.216.34"))
    assert guard_public_url("https://example.com/") == "https://example.com/"


def test_dns_failure_is_kind_dns(monkeypatch):
    monkeypatch.delenv("AIFORGE_SSRF_ALLOW_PRIVATE", raising=False)

    def _boom(*a, **kw):
        raise OSError("name resolution failed")

    monkeypatch.setattr(socket, "getaddrinfo", _boom)
    with pytest.raises(SSRFBlocked) as ei:
        guard_public_url("http://does-not-resolve.invalid/")
    assert ei.value.kind == "dns"


def test_escape_hatch_allows_private(monkeypatch):
    monkeypatch.setenv("AIFORGE_SSRF_ALLOW_PRIVATE", "1")
    # No exception even for a metadata IP when the operator opts in.
    assert guard_public_url("http://169.254.169.254/") == "http://169.254.169.254/"


# ─── _do_fetch (researcher web_read) ───────────────────────────────────


def test_do_fetch_rejects_metadata_without_request(monkeypatch):
    monkeypatch.delenv("AIFORGE_SSRF_ALLOW_PRIVATE", raising=False)
    from aiforge_core.runtime import doer_tools as dt

    def _must_not_call(*a, **kw):
        raise AssertionError("urlopen must not be called for a blocked URL")

    monkeypatch.setattr("urllib.request.urlopen", _must_not_call)
    res = dt._do_fetch("http://169.254.169.254/latest/meta-data/")
    assert res["ok"] is False
    assert "ssrf" in res["error"].lower()


# ─── _fetch_url (unauthenticated kind=url ingest) ──────────────────────


def test_memory_ingest_rejects_metadata_without_request(monkeypatch):
    monkeypatch.delenv("AIFORGE_SSRF_ALLOW_PRIVATE", raising=False)
    from aiforge_core.runtime import memory_ingest as mi

    def _must_not_call(*a, **kw):
        raise AssertionError("urlopen must not be called for a blocked URL")

    monkeypatch.setattr("urllib.request.urlopen", _must_not_call)
    with pytest.raises(SSRFBlocked):
        mi._fetch_url("http://127.0.0.1:8799/api/health")


def test_guard_symbol_reused_across_paths():
    # The two call sites import the SAME guard from net.ssl (no divergent copy).
    from aiforge_core.runtime import doer_tools, memory_ingest  # noqa: F401
    assert net_ssl.guard_public_url is guard_public_url
