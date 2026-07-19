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

import contextlib
import logging
import socket
import threading
import time

_log = logging.getLogger("aiforge.sync")

MCAST_ADDR = "239.255.255.250"
MCAST_PORT = 1900
SERVICE_TYPE = "urn:aiforge:service:memory-sync:1"
MAX_AGE = 1800

# One source may pull this many replies out of us per window. Small on purpose:
# a responder is only useful at a trickle, and a cap is what keeps a chatty or
# spoofing host from using us as a traffic multiplier.
RATE_LIMIT = 10
RATE_WINDOW = 10.0


def build_search() -> bytes:
    return (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {MCAST_ADDR}:{MCAST_PORT}\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 2\r\n"
        f"ST: {SERVICE_TYPE}\r\n"
        "\r\n"
    ).encode()


def _guard_crlf(peer_id: str, url: str) -> None:
    """Refuse header injection. One check, used by every builder that emits headers."""
    if any(c in peer_id or c in url for c in ("\r", "\n")):
        raise ValueError("peer_id and url must not contain CR or LF")


def build_announce(peer_id: str, url: str) -> bytes:
    _guard_crlf(peer_id, url)
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


def build_reply(peer_id: str, url: str) -> bytes:
    """The unicast ``200 OK`` answer to an ``M-SEARCH`` for our service type."""
    _guard_crlf(peer_id, url)
    return (
        "HTTP/1.1 200 OK\r\n"
        f"CACHE-CONTROL: max-age={MAX_AGE}\r\n"
        "EXT:\r\n"
        f"LOCATION: {url}\r\n"
        f"ST: {SERVICE_TYPE}\r\n"
        f"USN: uuid:{peer_id}::{SERVICE_TYPE}\r\n"
        "\r\n"
    ).encode()


def _headers(raw: bytes) -> dict:
    """Header lines of an SSDP datagram, upper-cased keys. Never raises."""
    try:
        text = raw.decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001 — arbitrary bytes arrive on a multicast socket
        return {}
    out: dict[str, str] = {}
    for line in text.split("\r\n"):
        if ":" in line:
            k, _, v = line.partition(":")
            out[k.strip().upper()] = v.strip()
    return out


def parse(raw: bytes) -> dict | None:
    """Extract ``{id, urls}`` from a datagram, or ``None`` if it is not ours."""
    headers = _headers(raw)
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


def _require_host(bind_host: str) -> None:
    """Refuse a wildcard interface address.

    Naming one LAN address rather than ``0.0.0.0`` is deliberate: SSDP is a
    well-known DDoS amplification vector, and traffic emitted from an interface
    nobody chose is how a helper becomes someone else's amplifier. This is a
    misconfiguration to fix, not a condition to degrade around, so it raises
    rather than falling back to a "safer" default.
    """
    if bind_host in ("0.0.0.0", "::", "", None):
        raise ValueError(f"refusing to use wildcard address for SSDP: {bind_host!r}")


def _multicast_socket(bind_host: str) -> socket.socket:
    """A multicast sender socket bound to one interface."""
    _require_host(bind_host)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    # Loopback of our own multicast is the default nearly everywhere, but it is
    # set explicitly so a responder in this same host answers our search: on one
    # machine that is the only path a datagram can take.
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
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


def _same_subnet(bind_host: str, sender_host: str) -> bool:
    """Is ``sender_host`` on our own /24?

    The whole risk in answering an SSDP search is amplification: an attacker
    spoofs a victim's source address, we reply, and the victim absorbs traffic
    it never asked for. That only pays off from off-link, because on-link the
    attacker can already see and send everything SSDP could leak. Refusing to
    answer anything outside our own segment removes the vector, and a /24 is the
    honest approximation — we hold an interface address, not a netmask, and a
    wider guess would be a wider hole.
    """
    try:
        a = bind_host.split(".")
        b = sender_host.split(".")
    except AttributeError:
        return False
    return len(a) == 4 and len(b) == 4 and a[:3] == b[:3]


class _RateLimiter:
    """Per-source reply cap. A dict of timestamps, pruned as it is used."""

    def __init__(self, limit: int = RATE_LIMIT, window: float = RATE_WINDOW):
        self.limit = limit
        self.window = window
        self._hits: dict[str, list[float]] = {}

    def allow(self, source: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        cutoff = now - self.window
        for key, stamps in list(self._hits.items()):
            fresh = [t for t in stamps if t > cutoff]
            if fresh:
                self._hits[key] = fresh
            else:
                del self._hits[key]
        stamps = self._hits.setdefault(source, [])
        if len(stamps) >= self.limit:
            return False
        stamps.append(now)
        return True


def _listener_socket(bind_host: str) -> socket.socket:
    """A socket that receives multicast searches on ``MCAST_PORT``.

    This binds the wildcard address on purpose, and that is *not* the case
    ``_multicast_socket`` refuses. A receiver must bind ``("", 1900)`` — bind a
    single address and the kernel does not deliver datagrams sent to the group
    address. The wildcard guard on the sender exists to stop us emitting from an
    unknown interface and becoming an off-link amplifier; here the equivalent
    protection is behavioural, in ``_same_subnet``: we listen everywhere but
    answer only our own segment. ``bind_host`` is still required and still
    validated, because it selects the interface that joins the group and it is
    the address ``_same_subnet`` reasons about.
    """
    _require_host(bind_host)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        # Not every platform honours it; sharing 1900 is a nicety, not a need.
        with contextlib.suppress(OSError):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.bind(("", MCAST_PORT))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                    socket.inet_aton(MCAST_ADDR) + socket.inet_aton(bind_host))
    return sock


def _should_reply(headers: dict, sender_host: str, bind_host: str, peer_id: str,
                  limiter: _RateLimiter) -> bool:
    """Decide whether one received datagram earns a reply. Pure, so it is testable."""
    st = headers.get("ST", "")
    # Exactly our own service type. Never ``ssdp:all`` or ``upnp:rootdevice``:
    # those are the classic amplification queries precisely because one small
    # request draws many large answers. One match, one reply, factor ~1:1.
    if st != SERVICE_TYPE:
        return False
    if not _same_subnet(bind_host, sender_host):
        _log.debug("sync: ssdp ignoring off-subnet search from %s", sender_host)
        return False
    usn = headers.get("USN", "")
    if peer_id and usn and usn.removeprefix("uuid:").split("::", 1)[0].strip() == peer_id:
        return False  # our own search, echoed back to us by multicast loopback
    if not limiter.allow(sender_host):
        _log.debug("sync: ssdp rate-limiting %s", sender_host)
        return False
    return True


def respond_forever(bind_host: str, peer_id: str, url: str, *,
                    stop: threading.Event | None = None) -> None:
    """Answer ``M-SEARCH`` for our service type until ``stop`` is set.

    Without this nothing ever replies to a search, so ``discover`` only ever
    caught an ``announce`` that happened to fire inside its listening window —
    a race, not a protocol.

    Never raises. The socket takes arbitrary bytes from anyone on the segment,
    so every failure here is a log line and another turn of the loop.
    """
    reply = build_reply(peer_id, url)  # built once; CR/LF-guarded, so fail loud early
    try:
        sock = _listener_socket(bind_host)
    except OSError as exc:  # no multicast here is normal, not an error
        _log.info("sync: ssdp responder unavailable on %s: %s", bind_host, exc)
        return
    limiter = _RateLimiter()
    _log.info("sync: ssdp responder listening on %s for %s", bind_host, SERVICE_TYPE)
    try:
        sock.settimeout(0.5)  # so a stop event is noticed promptly
        while stop is None or not stop.is_set():
            try:
                raw, addr = sock.recvfrom(4096)
            except TimeoutError:
                continue
            except OSError as exc:
                _log.info("sync: ssdp responder recv error: %s", exc)
                continue
            try:
                if _should_reply(_headers(raw), addr[0], bind_host, peer_id, limiter):
                    sock.sendto(reply, addr)
            except Exception as exc:  # noqa: BLE001 — a bad datagram must not end the loop
                _log.debug("sync: ssdp responder skipped a datagram: %s", exc)
    finally:
        sock.close()


def serve_in_background(bind_host: str, peer_id: str, url: str) -> threading.Event | None:
    """Run ``respond_forever`` on a daemon thread. ``None`` if SSDP is unusable here."""
    try:
        _require_host(bind_host)
        build_reply(peer_id, url)
    except ValueError as exc:
        _log.info("sync: ssdp responder not started: %s", exc)
        return None
    stop = threading.Event()
    thread = threading.Thread(target=respond_forever, args=(bind_host, peer_id, url),
                              kwargs={"stop": stop}, daemon=True,
                              name="aiforge-ssdp-responder")
    thread.start()
    return stop


__all__ = ["build_search", "build_announce", "build_reply", "parse", "discover",
           "announce", "respond_forever", "serve_in_background",
           "SERVICE_TYPE", "MCAST_ADDR", "MCAST_PORT"]
