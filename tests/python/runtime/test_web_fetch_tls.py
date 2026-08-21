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
    assert is_cert_error(ssl.SSLError("self signed certificate in chain"))


def test_a_network_failure_is_not_a_cert_error():
    """Retrying a refused connection or a timeout without verification buys
    nothing and would turn every outage into a silent security downgrade."""
    assert not is_cert_error(ConnectionRefusedError("refused"))
    assert not is_cert_error(TimeoutError("timed out"))
    assert not is_cert_error(urllib.error.HTTPError("u", 404, "nf", None, None))


def test_a_server_cannot_TALK_its_way_into_an_unverified_refetch():
    """`HTTPError.__str__` embeds the reason phrase straight from the server's
    status line, and an HTTP status means the handshake already SUCCEEDED. A
    server — or an on-path attacker who can inject plaintext but cannot forge a
    certificate — answering `502 certificate verify failed` must not talk the
    client into turning verification off."""
    assert not is_cert_error(
        urllib.error.HTTPError("u", 502, "certificate verify failed", None, None))
    assert not is_cert_error(
        urllib.error.HTTPError("u", 200, "self-signed certificate", None, None))
    # Nor may a non-transport exception spell its way in.
    assert not is_cert_error(ValueError("unable to get local issuer"))
    assert not is_cert_error(RuntimeError("certificate verify failed"))


def test_an_internal_host_is_never_refetched_unverified(monkeypatch):
    """The fallback is for the public web behind an inspecting appliance. A
    self-signed LAN service fails closed today; stripping verification would
    turn a model-supplied https://192.168.x.x/ from unreachable into readable —
    a reachability change, not a convenience."""
    from aiforge_core.net.ssl import web_tls_fallback_allowed_for as allowed
    assert allowed("https://example.com/page")
    assert not allowed("https://192.168.1.50/admin")
    assert not allowed("https://vault.internal/secret")
    assert not allowed("https://127.0.0.1:8443/")
    assert not allowed("https://box.local/")


def test_web_fetch_refuses_a_private_target(monkeypatch):
    """This path took a model-supplied URL straight to urlopen — no SSRF guard
    at all, unlike web_ingest. The TLS fallback made that materially worse."""
    monkeypatch.setenv("AIFORGE_ALLOW_WEB_FETCH", "1")
    monkeypatch.delenv("AIFORGE_WEB_SEARCH_DISABLE", raising=False)
    called: list = []
    monkeypatch.setattr(ws.urllib.request, "urlopen",
                        lambda *a, **k: called.append(1) or _Resp())
    out = ws.web_fetch({"url": "http://169.254.169.254/latest/meta-data/"})
    assert out["ok"] is False and "ssrf" in out["error"]
    assert not called


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
    # And it must actually VERIFY: asserting only "one attempt happened" let a
    # mutation that fetched everything with an unverified context pass.
    ctx = seen[0]
    assert ctx is None or (ctx.verify_mode == ssl.CERT_REQUIRED
                           and ctx.check_hostname is True)


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
    # Do not depend on real DNS: a network that NXDOMAIN-hijacks to a captive
    # portal makes the SSRF guard report a PRIVATE target and this test fail
    # for a reason that has nothing to do with TLS — on exactly the kind of
    # network this feature exists for.
    monkeypatch.setattr(_web, "guard_public_url", lambda _u: None, raising=False)
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


# ── the mutants an earlier version of this file did not catch ────────────


def test_the_env_gate_is_what_stops_the_retry_not_the_error(monkeypatch):
    """Asserting only "it raised" passed whether the gate worked or not: the
    insecure retry raises the same error when it also fails. Count the
    ATTEMPTS."""
    calls: list = []

    def _open(req, timeout=None, context=None):
        calls.append(context)
        raise _cert_error()

    monkeypatch.setattr(ws.urllib.request, "urlopen", _open)
    monkeypatch.setenv("AIFORGE_WEB_INSECURE_TLS", "0")
    with pytest.raises(urllib.error.URLError):
        ws._get("https://inspected.example.com")
    assert len(calls) == 1, "the gate must prevent the second attempt"

    calls.clear()
    monkeypatch.setenv("AIFORGE_WEB_INSECURE_TLS", "1")
    with pytest.raises(urllib.error.URLError):
        ws._get("https://inspected.example.com")
    assert len(calls) == 2


def test_the_doer_path_honours_the_same_gate(monkeypatch):
    from aiforge_core.runtime.doer_tools import _web

    calls: list = []

    def _open(req, timeout=None, context=None):
        calls.append(context)
        raise _cert_error()

    monkeypatch.setattr(_web.urllib.request, "urlopen", _open)
    monkeypatch.setattr(_web, "guard_public_url", lambda _u: None, raising=False)
    monkeypatch.setenv("AIFORGE_ALLOW_WEB_FETCH", "1")
    monkeypatch.setenv("AIFORGE_WEB_INSECURE_TLS", "0")
    out = _web.fetch_url("https://inspected.example.com")
    assert out["ok"] is False
    assert len(calls) == 1


def test_a_configured_ca_bundle_is_used_on_the_doer_path(monkeypatch, tmp_path):
    """The remedy the code recommends — install the inspecting appliance's CA —
    did nothing here: this path used the stdlib default and never consulted the
    bundle, so every page came back through the unverified fallback instead."""
    from aiforge_core.runtime.doer_tools import _web

    # A REAL bundle: ssl.create_default_context refuses an empty file, and a
    # helper that silently returns None on a bad path would make this test pass
    # while proving nothing.
    certifi = pytest.importorskip("certifi")
    monkeypatch.setenv("AIFORGE_LLM_CA_BUNDLE", certifi.where())
    seen: list = []

    class _R:
        status = 200
        url = None
        headers = {"Content-Type": "text/html"}
        def read(self, n=None):
            return b"<p>ok</p>"
        def __enter__(self):
            return self
        def __exit__(self, *_a):
            return False

    def _open(req, timeout=None, context=None):
        seen.append(context)
        return _R()

    monkeypatch.setattr(_web.urllib.request, "urlopen", _open)
    monkeypatch.setattr(_web, "guard_public_url", lambda _u: None, raising=False)
    monkeypatch.setenv("AIFORGE_ALLOW_WEB_FETCH", "1")
    _web.fetch_url("https://example.com")
    assert seen and seen[0] is not None, "the CA bundle was ignored"
    assert seen[0].verify_mode == ssl.CERT_REQUIRED


def test_the_crawl_dossier_records_an_unverified_page(monkeypatch, tmp_path):
    """web_crawl writes a dossier "so later sessions reuse it". A page fetched
    unverified whose note says nothing about it loses that provenance for
    every future reader."""
    from aiforge_core.runtime.tools import web_ingest

    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_ALLOW_WEB_FETCH", "1")
    monkeypatch.setenv("AIFORGE_WEB_CRAWLER", "fallback")
    from aiforge_core.runtime.tools import web_search as _ws_mod
    monkeypatch.setattr(_ws_mod, "_fetch_readable",
                        lambda url, n: {"ok": True, "title": "Docs",
                                        "text": "body text here",
                                        "tls_verified": False})
    out = web_ingest.web_crawl({"url": "https://inspected.example.com/docs"})
    assert out["ok"] and out["tls_verified"] is False
    import json as _json
    import os as _os
    meta = _json.loads(open(_os.path.join(
        _os.path.dirname(out["path"]), "meta.json")).read())
    assert meta.get("tls_verified") is False
    assert "NOT VERIFIED" in open(out["path"]).read()


def test_search_results_say_when_the_LIST_came_unverified(monkeypatch):
    """These urls are what the agent fetches next. An attacker-substitutable
    result set reported as an ordinary success is the worst place for silence."""
    calls: list = []

    def _open(req, timeout=None, context=None):
        calls.append(context)
        if len(calls) == 1:
            raise _cert_error()
        return _Resp(b'<a class="result__a" href="https://x.example/p">Hit</a>'
                     b'<a class="result__snippet">snip</a>')

    monkeypatch.delenv("AIFORGE_WEB_SEARCH_DISABLE", raising=False)
    monkeypatch.setattr(ws, "_api_search", lambda *a, **k: None)
    monkeypatch.setattr(ws.urllib.request, "urlopen", _open)
    out = ws.web_search({"query": "anything"})
    if out.get("ok") and out.get("results"):
        assert out.get("tls_verified") is False
