"""SSDP message construction and parsing. No sockets are opened here."""
from __future__ import annotations

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
