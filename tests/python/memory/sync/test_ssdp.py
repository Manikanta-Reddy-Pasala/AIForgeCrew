"""SSDP message construction and parsing. No sockets are opened here."""
from __future__ import annotations

from aiforge_core.memory.sync import discovery_ssdp as ssdp


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
