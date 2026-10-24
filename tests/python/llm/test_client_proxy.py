"""The cancellable model request (http.client) and the connect preflight go
through the environment's proxy — in the isolated sandbox the only way out —
and not around it."""
from __future__ import annotations

from types import SimpleNamespace

from aiforge_core.llm.client import _http as H


def _ep(url):
    return SimpleNamespace(base_url=url, extras={}, provider="openai_compatible", model="m",
                           api_key="")


def test_no_proxy_means_direct(monkeypatch):
    for v in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        monkeypatch.delenv(v, raising=False)
    assert H._proxy_for("https://api.example.com/v1") is None
    conn, path = H._open_connection(_ep("http://model:1234/v1"), "http://model:1234/v1/chat/completions", 5)
    assert (conn.host, conn.port, path) == ("model", 1234, "/v1/chat/completions")


def test_https_tunnels_through_the_proxy(monkeypatch):
    monkeypatch.setenv("https_proxy", "http://egress:8888")
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1,model")
    monkeypatch.setattr(H, "_post_ctx", lambda ep: None)
    assert H._proxy_for("https://api.example.com/v1") == ("egress", 8888)
    conn, path = H._open_connection(_ep("https://api.example.com/v1"),
                                    "https://api.example.com/v1/chat/completions", 5)
    assert (conn.host, conn.port) == ("egress", 8888)
    assert conn._tunnel_host == "api.example.com" and conn._tunnel_port == 443
    assert path == "/v1/chat/completions"


def test_plain_http_uses_the_absolute_form(monkeypatch):
    monkeypatch.setenv("http_proxy", "http://egress:8888")
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1,model")
    conn, path = H._open_connection(_ep("http://lan-model.example:8081/v1"),
                                    "http://lan-model.example:8081/v1/chat/completions?x=1", 5)
    assert (conn.host, conn.port) == ("egress", 8888)
    assert path == "http://lan-model.example:8081/v1/chat/completions?x=1"


def test_no_proxy_hosts_stay_direct(monkeypatch):
    monkeypatch.setenv("http_proxy", "http://egress:8888")
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1,model")
    assert H._proxy_for("http://model:1234/v1") is None
    assert H._proxy_for("http://127.0.0.1:1234/v1") is None


def test_preflight_probes_the_proxy(monkeypatch):
    monkeypatch.setenv("https_proxy", "http://egress:8888")
    monkeypatch.setenv("no_proxy", "model")
    seen = []

    class _S:
        def close(self):
            pass

    import socket
    monkeypatch.setattr(socket, "create_connection", lambda addr, timeout=None: seen.append(addr) or _S())
    from aiforge_core.llm import endpoint_breaker
    monkeypatch.setattr(endpoint_breaker, "is_open", lambda url: None)
    monkeypatch.setattr(endpoint_breaker, "record_success", lambda url: None)
    H._preflight("https://api.example.com/v1")
    assert seen == [("egress", 8888)]
