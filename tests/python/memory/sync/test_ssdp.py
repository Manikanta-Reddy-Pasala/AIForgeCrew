"""SSDP message construction and parsing, plus one real round trip over sockets."""
from __future__ import annotations

import socket
import time

import pytest

from aiforge_core.memory.sync import discovery_ssdp as ssdp


class _FakeSocket:
    """Stand-in for socket.socket that lets us force send/recv failures."""

    def __init__(self, sendto_error=None, recvfrom_error=None):
        self._sendto_error = sendto_error
        self._recvfrom_error = recvfrom_error

    def setsockopt(self, *a, **kw):
        pass

    def bind(self, *a, **kw):
        pass

    def settimeout(self, *a, **kw):
        pass

    def sendto(self, *a, **kw):
        if self._sendto_error:
            raise self._sendto_error
        return len(a[0]) if a else 0

    def recvfrom(self, *a, **kw):
        raise self._recvfrom_error if self._recvfrom_error else TimeoutError()

    def close(self):
        pass


def test_search_datagram_is_a_wellformed_m_search():
    msg = ssdp.build_search().decode()

    assert msg.startswith("M-SEARCH * HTTP/1.1\r\n")
    assert "HOST: 239.255.255.250:1900\r\n" in msg
    assert 'MAN: "ssdp:discover"\r\n' in msg
    assert f"ST: {ssdp.SERVICE_TYPE}\r\n" in msg
    assert msg.endswith("\r\n\r\n")


def test_announce_carries_location_and_usn():
    msg = ssdp.build_announce("nuc", "http://10.0.1.14:8799").decode()

    assert "LOCATION: http://10.0.1.14:8799\r\n" in msg
    assert f"USN: uuid:nuc::{ssdp.SERVICE_TYPE}\r\n" in msg
    assert "CACHE-CONTROL: max-age=" in msg


def test_parse_extracts_a_roster_entry():
    raw = ssdp.build_announce("nuc", "http://10.0.1.14:8799")

    assert ssdp.parse(raw) == {"id": "nuc", "urls": ["http://10.0.1.14:8799"]}


def test_parse_ignores_other_services():
    raw = (b"NOTIFY * HTTP/1.1\r\nLOCATION: http://x\r\n"
           b"USN: uuid:foo::urn:schemas-upnp-org:device:MediaServer:1\r\n\r\n")

    assert ssdp.parse(raw) is None


def test_parse_survives_garbage():
    assert ssdp.parse(b"\x00\xff not http at all") is None
    assert ssdp.parse(b"") is None


# --- finding #1: wildcard bind must be refused in code, not by convention ---

def test_multicast_socket_refuses_wildcard_bind():
    for bad in ("0.0.0.0", "::", ""):
        with pytest.raises(ValueError):
            ssdp._multicast_socket(bad)


def test_multicast_socket_refuses_none_bind():
    with pytest.raises(ValueError):
        ssdp._multicast_socket(None)


# --- finding #2: parse() must not pass through CR/LF-poisoned fields ---

def test_parse_rejects_embedded_lf_in_location():
    raw = (
        b"NOTIFY * HTTP/1.1\r\n"
        b"LOCATION: http://x\ny\r\n"
        b"NT: urn:aiforge:service:memory-sync:1\r\n"
        b"NTS: ssdp:alive\r\n"
        b"USN: uuid:foo::urn:aiforge:service:memory-sync:1\r\n"
        b"\r\n"
    )

    assert ssdp.parse(raw) is None


def test_parse_rejects_embedded_cr_in_peer_id():
    raw = (
        b"NOTIFY * HTTP/1.1\r\n"
        b"LOCATION: http://x\r\n"
        b"NT: urn:aiforge:service:memory-sync:1\r\n"
        b"NTS: ssdp:alive\r\n"
        b"USN: uuid:foo\rEvil-Header: 1::urn:aiforge:service:memory-sync:1\r\n"
        b"\r\n"
    )

    assert ssdp.parse(raw) is None


# --- finding #3: send/recv OSErrors must degrade gracefully, not propagate ---

def test_discover_returns_empty_list_on_send_error(monkeypatch):
    monkeypatch.setattr(
        ssdp.socket, "socket",
        lambda *a, **kw: _FakeSocket(sendto_error=OSError("network unreachable")),
    )

    assert ssdp.discover("10.0.1.5", timeout=0.01) == []


def test_discover_returns_empty_list_on_recv_error(monkeypatch):
    monkeypatch.setattr(
        ssdp.socket, "socket",
        lambda *a, **kw: _FakeSocket(recvfrom_error=OSError("network unreachable")),
    )

    assert ssdp.discover("10.0.1.5", timeout=0.01) == []


def test_announce_returns_false_on_send_error(monkeypatch):
    monkeypatch.setattr(
        ssdp.socket, "socket",
        lambda *a, **kw: _FakeSocket(sendto_error=OSError("network unreachable")),
    )

    assert ssdp.announce("10.0.1.5", "nuc", "http://x") is False


def test_wildcard_bind_valueerror_is_not_swallowed_by_oserror_handling(monkeypatch):
    # Even if socket() would otherwise succeed, the wildcard guard raises before
    # any socket is touched, and it must propagate out of discover()/announce()
    # rather than being caught by the OSError handling around socket setup.
    monkeypatch.setattr(ssdp.socket, "socket", lambda *a, **kw: _FakeSocket())

    with pytest.raises(ValueError):
        ssdp.discover("0.0.0.0", timeout=0.01)
    with pytest.raises(ValueError):
        ssdp.announce("0.0.0.0", "nuc", "http://x")


# --- finding #4: build_announce() must reject CR/LF header injection ---

def test_build_announce_rejects_crlf_in_peer_id():
    with pytest.raises(ValueError):
        ssdp.build_announce("nuc\r\nEVIL: header", "http://10.0.1.14:8799")


def test_build_announce_rejects_crlf_in_url():
    with pytest.raises(ValueError):
        ssdp.build_announce("nuc", "http://10.0.1.14:8799\r\nEVIL: header")


# --- finding #5 (live validation): nothing answered an M-SEARCH ---

def test_reply_is_a_wellformed_200_ok():
    msg = ssdp.build_reply("nuc", "http://10.0.1.14:8799").decode()

    assert msg.startswith("HTTP/1.1 200 OK\r\n")
    assert "CACHE-CONTROL: max-age=1800\r\n" in msg
    assert "EXT:\r\n" in msg
    assert "LOCATION: http://10.0.1.14:8799\r\n" in msg
    assert f"ST: {ssdp.SERVICE_TYPE}\r\n" in msg
    assert f"USN: uuid:nuc::{ssdp.SERVICE_TYPE}\r\n" in msg
    assert msg.endswith("\r\n\r\n")
    assert "\n" not in msg.replace("\r\n", "")


def test_reply_round_trips_through_parse():
    raw = ssdp.build_reply("nuc", "http://10.0.1.14:8799")

    assert ssdp.parse(raw) == {"id": "nuc", "urls": ["http://10.0.1.14:8799"]}


def test_build_reply_rejects_crlf():
    with pytest.raises(ValueError):
        ssdp.build_reply("nuc\r\nEVIL: header", "http://x")
    with pytest.raises(ValueError):
        ssdp.build_reply("nuc", "http://x\r\nEVIL: header")


def _search(st=ssdp.SERVICE_TYPE, usn=None):
    lines = ["M-SEARCH * HTTP/1.1", 'MAN: "ssdp:discover"', f"ST: {st}"]
    if usn:
        lines.append(f"USN: {usn}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


def _fresh_limiter():
    return ssdp._RateLimiter()


def test_subnet_check_accepts_on_link_and_rejects_off_link():
    assert ssdp._same_subnet("10.0.1.5", "10.0.1.99") is True
    assert ssdp._same_subnet("10.0.1.5", "10.0.2.99") is False
    assert ssdp._same_subnet("10.0.1.5", "203.0.113.7") is False


def test_off_subnet_search_is_not_answered():
    headers = ssdp._headers(_search())

    assert ssdp._should_reply(headers, "203.0.113.7", "10.0.1.5", "nuc",
                              _fresh_limiter()) is False


def test_on_subnet_search_for_our_service_is_answered():
    headers = ssdp._headers(_search())

    assert ssdp._should_reply(headers, "10.0.1.9", "10.0.1.5", "nuc",
                              _fresh_limiter()) is True


def test_ssdp_all_is_never_answered():
    # The classic amplification query: one small request, many large answers.
    for st in ("ssdp:all", "upnp:rootdevice", ""):
        headers = ssdp._headers(_search(st=st))
        assert ssdp._should_reply(headers, "10.0.1.9", "10.0.1.5", "nuc",
                                  _fresh_limiter()) is False


def test_our_own_search_is_ignored():
    headers = ssdp._headers(_search(usn=f"uuid:nuc::{ssdp.SERVICE_TYPE}"))

    assert ssdp._should_reply(headers, "10.0.1.9", "10.0.1.5", "nuc",
                              _fresh_limiter()) is False


def test_amplification_factor_is_stated_honestly_in_the_source():
    """The reply-size comment claimed "factor ~1:1"; it is ~4.7x.

    The exposure is genuinely small — on-link only and rate-limited — but a
    security comment that understates a factor by 4.7x is worse than no comment,
    because the next reader budgets against it. Measure it here so the claim
    cannot rot again.
    """
    import inspect

    # The smallest datagram this responder will answer: _headers needs no
    # request line, only an ST header carrying our service type.
    smallest = f"ST: {ssdp.SERVICE_TYPE}".encode()
    assert ssdp._should_reply(ssdp._headers(smallest), "10.0.1.9", "10.0.1.5",
                              "nuc", _fresh_limiter()) is True

    factor = len(ssdp.build_reply("nuc", "http://10.0.1.14:8799")) / len(smallest)
    assert 4.0 < factor < 5.5, factor

    source = inspect.getsource(ssdp)
    assert "factor ~1:1" not in source          # the false claim is gone
    assert "~4.7x" in source                    # the measured one is stated
    assert "RATE_LIMIT" in source.split("_should_reply", 1)[1][:1500]


def test_rate_limiter_caps_a_chatty_source():
    limiter = _fresh_limiter()
    headers = ssdp._headers(_search())

    answered = sum(ssdp._should_reply(headers, "10.0.1.9", "10.0.1.5", "nuc", limiter)
                   for _ in range(50))

    assert answered == ssdp.RATE_LIMIT


def test_rate_limiter_forgets_a_source_after_the_window():
    limiter = ssdp._RateLimiter(limit=2, window=10.0)

    assert limiter.allow("10.0.1.9", now=100.0) is True
    assert limiter.allow("10.0.1.9", now=100.1) is True
    assert limiter.allow("10.0.1.9", now=100.2) is False
    assert limiter.allow("10.0.1.9", now=200.0) is True
    assert limiter._hits.keys() == {"10.0.1.9"}  # stale entries pruned, not hoarded


def test_rate_limit_is_per_source():
    limiter = ssdp._RateLimiter(limit=1)

    assert limiter.allow("10.0.1.9") is True
    assert limiter.allow("10.0.1.9") is False
    assert limiter.allow("10.0.1.10") is True


def test_responder_refuses_a_wildcard_bind():
    assert ssdp.serve_in_background("0.0.0.0", "nuc", "http://x") is None
    assert ssdp.serve_in_background("", "nuc", "http://x") is None


def test_responder_refuses_crlf_poisoned_identity():
    assert ssdp.serve_in_background("10.0.1.5", "nuc\r\nEVIL: 1", "http://x") is None


# --- the test that would have caught the original gap: real sockets, one host ---

def _lan_ip() -> str:
    """This host's primary IPv4 address. No packet is sent by connect() on UDP."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 53))
        return sock.getsockname()[0]
    except OSError:
        return ""
    finally:
        sock.close()


def test_discover_finds_a_live_responder_on_this_host():
    ip = _lan_ip()
    if not ip or ip.startswith("127."):
        pytest.skip("no routable IPv4 interface: this host cannot do multicast at all")
    try:
        probe = ssdp._listener_socket(ip)
    except OSError as exc:
        pytest.skip(f"cannot bind/join SSDP multicast on {ip}: {exc}")
    probe.close()

    stop = ssdp.serve_in_background(ip, "responder-under-test", "http://127.0.0.1:8799")
    assert stop is not None
    try:
        time.sleep(0.3)  # let the thread reach recvfrom before we search
        found = ssdp.discover(ip, timeout=2.0)
    finally:
        stop.set()

    assert {"id": "responder-under-test", "urls": ["http://127.0.0.1:8799"]} in found
