"""The browser's egress proxy, on real loopback sockets (no browser)."""
from __future__ import annotations

import socket
import threading

import pytest

from aiforge_core.runtime.tools import _egress_proxy as ep


@pytest.fixture
def upstream():
    """A loopback server that records what it receives and answers once."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    got: list[bytes] = []

    def serve():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            c.settimeout(2)
            try:
                got.append(c.recv(4096))
                c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
            except OSError:
                pass
            c.close()

    threading.Thread(target=serve, daemon=True).start()
    yield srv.getsockname()[1], got
    srv.close()


@pytest.fixture
def proxy():
    blocked: list[str] = []
    allowed = {"127.0.0.1"}
    p = ep.EgressProxy(allow=lambda h, port: h in allowed,
                       on_block=blocked.append)
    p.start()
    yield p, blocked
    p.stop()


def _auth(p) -> str:
    import base64
    tok = base64.b64encode(f"{p.username}:{p.password}".encode()).decode()
    return f"Proxy-Authorization: Basic {tok}\r\n"


def _ask(port: int, raw: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=3) as s:
        s.sendall(raw)
        out = b""
        while True:
            try:
                chunk = s.recv(4096)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
    return out


def test_connect_to_an_allowed_host_is_tunnelled(proxy, upstream):
    p, _ = proxy
    up_port, got = upstream
    with socket.create_connection(("127.0.0.1", p.port), timeout=3) as s:
        s.sendall(f"CONNECT 127.0.0.1:{up_port} HTTP/1.1\r\n{_auth(p)}\r\n".encode())
        assert s.recv(4096).startswith(b"HTTP/1.1 200")
        s.sendall(b"PING-THROUGH-TUNNEL")
        assert s.recv(4096).startswith(b"HTTP/1.1 200 OK")
    assert got == [b"PING-THROUGH-TUNNEL"]


def test_connect_to_a_refused_host_gets_403_and_is_reported(proxy):
    p, blocked = proxy
    out = _ask(p.port, f"CONNECT evil.example:443 HTTP/1.1\r\n{_auth(p)}\r\n".encode())
    assert out.startswith(b"HTTP/1.1 403")
    assert blocked == ["https://evil.example:443/"]


def test_plain_http_is_rewritten_to_one_request_per_connection(proxy, upstream):
    p, _ = proxy
    up_port, got = upstream
    out = _ask(p.port, (f"GET http://127.0.0.1:{up_port}/a?b=1 HTTP/1.1\r\n"
                        f"Host: 127.0.0.1:{up_port}\r\nProxy-Connection: keep-alive\r\n"
                        f"{_auth(p)}\r\n").encode())
    assert out.endswith(b"ok")
    sent = got[0].decode()
    assert sent.startswith("GET /a?b=1 HTTP/1.1\r\n")
    assert "Proxy-" not in sent
    assert "Connection: close" in sent


def test_plain_http_to_a_refused_host_is_403(proxy):
    p, blocked = proxy
    out = _ask(p.port, (f"GET http://evil.example/x HTTP/1.1\r\nHost: evil.example\r\n"
                        f"{_auth(p)}\r\n").encode())
    assert out.startswith(b"HTTP/1.1 403")
    assert blocked == ["http://evil.example:80/"]


@pytest.mark.parametrize("ip", ["169.254.169.254", "::ffff:169.254.169.254",
                                "fd00:ec2::254", "fe80::1%eth0"])
def test_a_name_resolving_to_metadata_is_refused_whatever_the_allowlist(monkeypatch, ip):
    monkeypatch.setattr(ep.socket, "getaddrinfo",
                        lambda *a, **k: [(0, 0, 0, "", (ip, 80))])
    assert ep._resolve("metadata.example", 80) is None


def test_lan_addresses_resolve(monkeypatch):
    monkeypatch.setattr(ep.socket, "getaddrinfo",
                        lambda *a, **k: [(0, 0, 0, "", ("10.0.0.5", 80))])
    assert ep._resolve("devbox", 80) == "10.0.0.5"


def test_a_client_without_the_proxy_credentials_gets_407(proxy, upstream):
    # Any local process could otherwise borrow the browser's egress policy.
    p, _ = proxy
    up_port, got = upstream
    for auth in ("", "Proxy-Authorization: Basic eDp5\r\n"):
        out = _ask(p.port, f"CONNECT 127.0.0.1:{up_port} HTTP/1.1\r\n{auth}\r\n".encode())
        assert out.startswith(b"HTTP/1.1 407")
    assert got == []


def test_a_bad_port_is_400_not_a_traceback(proxy):
    p, _ = proxy
    out = _ask(p.port, (f"GET http://127.0.0.1:99999/ HTTP/1.1\r\n{_auth(p)}\r\n").encode())
    assert out.startswith(b"HTTP/1.1 400")


def test_a_host_with_odd_characters_is_refused(proxy):
    p, blocked = proxy
    out = _ask(p.port, f"CONNECT allowed.com#.evil.com:443 HTTP/1.1\r\n{_auth(p)}\r\n".encode())
    assert out.startswith(b"HTTP/1.1 403")


def test_stop_frees_a_client_that_never_sent_a_head():
    import time
    p = ep.EgressProxy(allow=lambda h, port: True, on_block=lambda u: None)
    p.start()
    idle = socket.create_connection(("127.0.0.1", p.port), timeout=3)
    time.sleep(0.2)
    p.stop()
    idle.settimeout(3)
    assert idle.recv(10) == b""        # the proxy closed it, nothing hangs
    idle.close()


def test_an_allow_callback_that_raises_fails_closed(upstream):
    up_port, _ = upstream
    blocked: list[str] = []

    def boom(h, port):
        raise RuntimeError("x")

    p = ep.EgressProxy(allow=boom, on_block=blocked.append)
    p.start()
    try:
        out = _ask(p.port, f"CONNECT 127.0.0.1:{up_port} HTTP/1.1\r\n{_auth(p)}\r\n".encode())
    finally:
        p.stop()
    assert out.startswith(b"HTTP/1.1 403")


@pytest.mark.parametrize("target,expect", [
    ("example.com:443", ("example.com", 443)),
    ("[::1]:8443", ("::1", 8443)),
    ("example.com", ("example.com", 443)),
    ("example.com:x", ("", 0)),
])
def test_split_host_port(target, expect):
    assert ep._split_host_port(target, 443) == expect
