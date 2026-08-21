"""A broken certificate chain must not make the web unreadable — and must not
quietly make every fetch insecure either.

A network that inspects TLS (a corporate appliance) re-signs every response
with a CA this process does not trust, so an ordinary public page fails with
CERTIFICATE_VERIFY_FAILED and the agent is blind. The rule: verify FIRST, fall
back only on a certificate error, and report the downgrade.
"""
from __future__ import annotations

import ssl
import urllib.error

import pytest

from aiforge_core.net.ssl import is_cert_error, web_tls_fallback_enabled
from aiforge_core.runtime.tools import web_search as ws


def _cert_error():
    return urllib.error.URLError(
        ssl.SSLCertVerificationError(
            "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
            "unable to get local issuer certificate"))


class _Resp:
    def __init__(self, body=b"<title>hi</title><p>page</p>"):
        self._b = body
    def read(self, n=None):
        return self._b
    @property
    def headers(self):
        return {}
    def __enter__(self):
        return self
    def __exit__(self, *_a):
        return False


def test_a_cert_error_is_recognised_through_its_wrappers():
    assert is_cert_error(_cert_error())
    assert is_cert_error(ssl.SSLCertVerificationError("certificate verify failed"))
    assert is_cert_error(RuntimeError("self signed certificate in chain"))


def test_a_network_failure_is_not_a_cert_error():
    """Retrying a refused connection or a timeout without verification buys
    nothing and would turn every outage into a silent security downgrade."""
    assert not is_cert_error(ConnectionRefusedError("refused"))
    assert not is_cert_error(TimeoutError("timed out"))
    assert not is_cert_error(urllib.error.HTTPError("u", 404, "nf", None, None))


def test_the_verified_attempt_comes_first(monkeypatch):
    seen: list = []

    def _open(req, timeout=None, context=None):
        seen.append(context)
        return _Resp()

    monkeypatch.setattr(ws.urllib.request, "urlopen", _open)
    verified: list = []
    ws._get("https://example.com", verified=verified)
    assert len(seen) == 1                    # no second, unverified attempt
    assert verified == [True]


def test_a_cert_failure_refetches_without_verification(monkeypatch):
    calls: list = []

    def _open(req, timeout=None, context=None):
        calls.append(context)
        if len(calls) == 1:
            raise _cert_error()
        return _Resp()

    monkeypatch.setattr(ws.urllib.request, "urlopen", _open)
    verified: list = []
    out = ws._get("https://inspected.example.com", verified=verified)
    assert "page" in out
    assert len(calls) == 2
    assert calls[1].verify_mode == ssl.CERT_NONE
    assert verified == [False], "the downgrade must be reported, not hidden"


def test_the_fallback_can_be_forbidden(monkeypatch):
    monkeypatch.setenv("AIFORGE_WEB_INSECURE_TLS", "0")
    assert not web_tls_fallback_enabled()

    def _open(req, timeout=None, context=None):
        raise _cert_error()

    monkeypatch.setattr(ws.urllib.request, "urlopen", _open)
    with pytest.raises(urllib.error.URLError):
        ws._get("https://inspected.example.com")


def test_a_non_cert_failure_is_never_retried_insecurely(monkeypatch):
    calls: list = []

    def _open(req, timeout=None, context=None):
        calls.append(context)
        raise ConnectionRefusedError("refused")

    monkeypatch.setattr(ws.urllib.request, "urlopen", _open)
    with pytest.raises(ConnectionRefusedError):
        ws._get("https://down.example.com")
    assert len(calls) == 1


def test_web_fetch_reports_an_unverified_page(monkeypatch):
    calls: list = []

    def _open(req, timeout=None, context=None):
        calls.append(context)
        if len(calls) == 1:
            raise _cert_error()
        return _Resp()

    monkeypatch.setenv("AIFORGE_ALLOW_WEB_FETCH", "1")
    monkeypatch.delenv("AIFORGE_WEB_SEARCH_DISABLE", raising=False)
    monkeypatch.setattr(ws.urllib.request, "urlopen", _open)
    out = ws.web_fetch({"url": "https://inspected.example.com"})
    assert out["ok"] and out["tls_verified"] is False


def test_a_verified_page_says_nothing_about_tls(monkeypatch):
    """"tls_verified: true" on every ordinary fetch is noise the model would
    carry in its context forever. The exception is the thing worth stating."""
    monkeypatch.setenv("AIFORGE_ALLOW_WEB_FETCH", "1")
    monkeypatch.delenv("AIFORGE_WEB_SEARCH_DISABLE", raising=False)
    monkeypatch.setattr(ws.urllib.request,
                        "urlopen", lambda *a, **k: _Resp())
    out = ws.web_fetch({"url": "https://example.com"})
    assert out["ok"] and "tls_verified" not in out


def test_plain_http_is_still_allowed(monkeypatch):
    """"Allow non-secure" — an http:// URL is fetched as-is; there is no
    certificate in play and nothing to fall back from."""
    monkeypatch.setenv("AIFORGE_ALLOW_WEB_FETCH", "1")
    monkeypatch.delenv("AIFORGE_WEB_SEARCH_DISABLE", raising=False)
    monkeypatch.setattr(ws.urllib.request, "urlopen", lambda *a, **k: _Resp())
    out = ws.web_fetch({"url": "http://intranet.local/page"})
    assert out["ok"] and "tls_verified" not in out


# ── the doer / researcher path, which has its own fetch ──────────────────


def test_the_doer_fetch_falls_back_the_same_way(monkeypatch):
    """Two fetch implementations exist; a rule applied to one of them is a rule
    the agent meets half the time."""
    from aiforge_core.runtime.doer_tools import _web

    calls: list = []

    def _open(req, timeout=None, context=None):
        calls.append(context)
        if len(calls) == 1:
            raise _cert_error()
        class _R:
            status = 200
            url = None
            headers = {"Content-Type": "text/html"}
            def read(self, n=None):
                return b"<p>page</p>"
            def __enter__(self):
                return self
            def __exit__(self, *_a):
                return False
        return _R()

    monkeypatch.setattr(_web.urllib.request, "urlopen", _open)
    monkeypatch.setenv("AIFORGE_ALLOW_WEB_FETCH", "1")
    out = _web.fetch_url("https://inspected.example.com")
    assert out["ok"] and out["tls_verified"] is False
    assert calls[1].verify_mode == ssl.CERT_NONE


def test_the_doer_fetch_does_not_retry_a_404(monkeypatch):
    from aiforge_core.runtime.doer_tools import _web

    calls: list = []

    def _open(req, timeout=None, context=None):
        calls.append(context)
        raise urllib.error.HTTPError("u", 404, "not found", None, None)

    monkeypatch.setattr(_web.urllib.request, "urlopen", _open)
    monkeypatch.setenv("AIFORGE_ALLOW_WEB_FETCH", "1")
    out = _web.fetch_url("https://example.com/missing")
    assert out["ok"] is False and out["status"] == 404
    assert len(calls) == 1
