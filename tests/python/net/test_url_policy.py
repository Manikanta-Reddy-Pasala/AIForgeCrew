"""The one place that decides which URLs we talk to.

The rule the module claims: https always; plain http only to a LOOPBACK host;
``AIFORGE_REQUIRE_HTTPS=1`` removes even that exception. These tests pin the
loopback carve-out in particular — it is the part a "harden the scheme check"
edit is most likely to delete, and deleting it breaks every local model stack
(LM Studio :1234, the embed sidecar :8764, ollama :11434), all of which are
plain http on 127.0.0.1.
"""
from __future__ import annotations

import pytest

from aiforge_core.net.url_policy import check, is_allowed, is_loopback


@pytest.fixture(autouse=True)
def _lax(monkeypatch):
    """Default posture: the strict flag unset, so loopback http is allowed."""
    monkeypatch.delenv("AIFORGE_REQUIRE_HTTPS", raising=False)


# ─── is_loopback ───────────────────────────────────────────────────────


@pytest.mark.parametrize("host", [
    "localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0",
    "host.docker.internal", "127.0.0.53", "LOCALHOST",
])
def test_loopback_hosts(host):
    assert is_loopback(host) is True


@pytest.mark.parametrize("host", ["example.com", "10.0.0.5", "8.8.8.8", "", None])
def test_routable_hosts_are_not_loopback(host):
    assert is_loopback(host) is False


# ─── check ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("url", [
    "https://api.anthropic.com/v1/messages",
    "http://127.0.0.1:1234/v1/chat/completions",
    "http://localhost:8764/embed",
    "http://[::1]:11434/api/tags",
    "http://host.docker.internal:8080/",
])
def test_allowed(url):
    assert check(url) is None
    assert is_allowed(url) is True


def test_plain_http_to_a_routable_host_is_refused():
    msg = check("http://example.com/v1")
    assert msg is not None
    assert "example.com" in msg
    assert "cleartext" in msg
    assert is_allowed("http://example.com/v1") is False


@pytest.mark.parametrize("url", ["", "   ", None])
def test_empty_url(url):
    assert check(url) == "missing url"


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://host/x", "ws://host/x", "example.com/x"])
def test_unsupported_scheme(url):
    msg = check(url)
    assert msg is not None
    assert "unsupported scheme" in msg


def test_unsupported_scheme_names_the_loopback_exception_only_when_it_exists(monkeypatch):
    assert "loopback" in check("ftp://host/x")
    monkeypatch.setenv("AIFORGE_REQUIRE_HTTPS", "1")
    assert "loopback" not in check("ftp://host/x")


def test_unparseable_url():
    # An unmatched IPv6 bracket makes urlsplit raise rather than return.
    msg = check("http://[::1/x")
    assert msg is not None
    assert "unparseable url" in msg


# ─── strict mode ───────────────────────────────────────────────────────


@pytest.mark.parametrize("flag", ["1", "true", "TRUE", "yes", "on"])
def test_strict_flag_refuses_even_loopback_http(monkeypatch, flag):
    monkeypatch.setenv("AIFORGE_REQUIRE_HTTPS", flag)
    msg = check("http://127.0.0.1:1234/v1")
    assert msg is not None
    assert "AIFORGE_REQUIRE_HTTPS=1" in msg


@pytest.mark.parametrize("flag", ["0", "false", "no", "", "off"])
def test_strict_flag_off_keeps_the_loopback_exception(monkeypatch, flag):
    monkeypatch.setenv("AIFORGE_REQUIRE_HTTPS", flag)
    assert check("http://127.0.0.1:1234/v1") is None


def test_strict_flag_never_refuses_https(monkeypatch):
    monkeypatch.setenv("AIFORGE_REQUIRE_HTTPS", "1")
    assert check("https://api.example.com/v1") is None
