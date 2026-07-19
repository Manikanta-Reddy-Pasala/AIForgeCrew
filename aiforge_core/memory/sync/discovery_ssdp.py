"""Zero-config peer discovery on the local segment via SSDP.

Chosen over mDNS/DNS-SD because it needs no record marshalling and no library:
the payload is HTTP-shaped text over UDP multicast, and ``LOCATION`` already
carries a url.

This can only ever be a convenience. Multicast is link-local — small TTL,
dropped by routers and access points — and WireGuard is a routed L3 tunnel with
no broadcast domain, so SSDP fails between two of your own machines the moment
they talk over the tunnel. Gossip over the manifest carries the mesh; SSDP just
saves typing a seed url when two peers share a physical segment.

Discovered peers land in ``candidate`` state exactly like gossiped ones. SSDP is
unauthenticated and trivially spoofable, so it must never confer trust.
"""
from __future__ import annotations

import logging
import socket

_log = logging.getLogger("aiforge.sync")

MCAST_ADDR = "239.255.255.250"
MCAST_PORT = 1900
SERVICE_TYPE = "urn:aiforge:service:memory-sync:1"
MAX_AGE = 1800


def build_search() -> bytes:
    return (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {MCAST_ADDR}:{MCAST_PORT}\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 2\r\n"
        f"ST: {SERVICE_TYPE}\r\n"
        "\r\n"
    ).encode()


def build_announce(peer_id: str, url: str) -> bytes:
    if any(c in peer_id or c in url for c in ("\r", "\n")):
        raise ValueError("peer_id and url must not contain CR or LF")
    return (
        "NOTIFY * HTTP/1.1\r\n"
        f"HOST: {MCAST_ADDR}:{MCAST_PORT}\r\n"
        f"CACHE-CONTROL: max-age={MAX_AGE}\r\n"
        f"LOCATION: {url}\r\n"
        f"NT: {SERVICE_TYPE}\r\n"
        "NTS: ssdp:alive\r\n"
        f"USN: uuid:{peer_id}::{SERVICE_TYPE}\r\n"
        "\r\n"
    ).encode()


def parse(raw: bytes) -> dict | None:
    """Extract ``{id, urls}`` from a datagram, or ``None`` if it is not ours."""
    try:
        text = raw.decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001 — arbitrary bytes arrive on a multicast socket
        return None
    headers: dict[str, str] = {}
    for line in text.split("\r\n"):
        if ":" in line:
            k, _, v = line.partition(":")
            headers[k.strip().upper()] = v.strip()
    usn = headers.get("USN", "")
    if SERVICE_TYPE not in usn:
        return None
    location = headers.get("LOCATION", "")
    peer_id = usn.removeprefix("uuid:").split("::", 1)[0].strip()
    if not peer_id or not location:
        return None
    if any(c in peer_id or c in location for c in ("\r", "\n")):
        return None
    return {"id": peer_id, "urls": [location]}


def _multicast_socket(bind_host: str) -> socket.socket:
    """A multicast socket bound to one interface.

    Binding to a specific LAN address rather than ``0.0.0.0`` is deliberate:
    SSDP responders are a well-known DDoS amplification vector, and a responder
    reachable beyond the local segment becomes someone else's amplifier. This is
    a misconfiguration to fix, not a condition to degrade around, so it raises
    rather than falling back to a safer bind.
    """
    if bind_host in ("0.0.0.0", "::", "", None):
        raise ValueError(f"refusing to bind SSDP socket to wildcard address: {bind_host!r}")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                    socket.inet_aton(bind_host))
    sock.bind((bind_host, 0))
    return sock


def discover(bind_host: str, timeout: float = 3.0) -> list[dict]:
    """Multicast a search and collect replies for ``timeout`` seconds."""
    found: dict[str, dict] = {}
    try:
        sock = _multicast_socket(bind_host)
    except OSError as exc:  # no multicast here is normal, not an error
        _log.info("sync: ssdp unavailable on %s: %s", bind_host, exc)
        return []
    try:
        sock.settimeout(timeout)
        sock.sendto(build_search(), (MCAST_ADDR, MCAST_PORT))
        while True:
            try:
                raw, _addr = sock.recvfrom(4096)
            except TimeoutError:
                break
            entry = parse(raw)
            if entry:
                found[entry["id"]] = entry
    except OSError as exc:  # e.g. ENETUNREACH is normal, not an error
        _log.info("sync: ssdp send/recv error on %s: %s", bind_host, exc)
        return []
    finally:
        sock.close()
    return list(found.values())


def announce(bind_host: str, peer_id: str, url: str) -> bool:
    try:
        sock = _multicast_socket(bind_host)
    except OSError as exc:  # no multicast here is normal, not an error
        _log.info("sync: ssdp announce unavailable on %s: %s", bind_host, exc)
        return False
    try:
        sock.sendto(build_announce(peer_id, url), (MCAST_ADDR, MCAST_PORT))
        return True
    except OSError as exc:  # e.g. ENETUNREACH is normal, not an error
        _log.info("sync: ssdp announce send error on %s: %s", bind_host, exc)
        return False
    finally:
        sock.close()


__all__ = ["build_search", "build_announce", "parse", "discover", "announce",
           "SERVICE_TYPE", "MCAST_ADDR", "MCAST_PORT"]
