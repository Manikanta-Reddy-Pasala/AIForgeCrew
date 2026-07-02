"""Browser allowlist must match the HOSTNAME (not a substring of the whole
URL) and deny private/metadata targets regardless of the allowlist.

The old ``re.search(pattern, url)`` let ``http://169.254.169.254/#github.com``
slip past a ``github.com`` allowlist (SSRF to cloud metadata).
"""
from __future__ import annotations

import socket

from aiforge_core.runtime.tools import browser


def _fake_getaddrinfo(ip: str):
    def _inner(host, port, *a, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port or 0))]
    return _inner


def test_metadata_url_blocked_despite_allowlist_token(monkeypatch):
    monkeypatch.delenv("AIFORGE_SSRF_ALLOW_PRIVATE", raising=False)
    monkeypatch.setenv("AIFORGE_BROWSER_ALLOWLIST", "github.com")
    # 169.254.169.254 is an IP literal → private guard fires before allowlist.
    assert browser._allowlist_ok("http://169.254.169.254/#github.com") is False


def test_path_smuggled_token_blocked(monkeypatch):
    monkeypatch.delenv("AIFORGE_SSRF_ALLOW_PRIVATE", raising=False)
    monkeypatch.setenv("AIFORGE_BROWSER_ALLOWLIST", "github.com")
    # evil.com resolves public but its host != github.com → denied.
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("93.184.216.34"))
    assert browser._allowlist_ok("http://evil.com/github.com") is False


def test_subdomain_of_allowlisted_host_allowed(monkeypatch):
    monkeypatch.delenv("AIFORGE_SSRF_ALLOW_PRIVATE", raising=False)
    monkeypatch.setenv("AIFORGE_BROWSER_ALLOWLIST", "github.com")
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("140.82.112.3"))
    assert browser._allowlist_ok("https://api.github.com/repos/x") is True


def test_exact_host_allowed(monkeypatch):
    monkeypatch.delenv("AIFORGE_SSRF_ALLOW_PRIVATE", raising=False)
    monkeypatch.setenv("AIFORGE_BROWSER_ALLOWLIST", "github.com")
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("140.82.112.3"))
    assert browser._allowlist_ok("https://github.com/x") is True
