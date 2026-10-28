"""A tiny forward proxy that every non-loopback browser connection goes through.

A Playwright ``route`` handler sees requests, not connections: it misses the
second hop of a redirect, WebSockets opened from a worker, ``preconnect``,
WebRTC and anything else Chromium does below the request layer. The browser
therefore runs with this proxy, one per browser context, and the proxy decides
on every CONNECT and every absolute-URI request whether the host may be
reached. Loopback stays direct (Chromium bypasses the proxy for it), which is
fine: loopback is not egress.

Bytes are piped, never buffered, so streaming (SSE, long polls, downloads,
WebSockets) behaves exactly as without the proxy.
"""
from __future__ import annotations

import base64
import contextlib
import hmac
import ipaddress
import logging
import re
import secrets
import selectors
import socket
import socketserver
import threading
from collections.abc import Callable
from urllib.parse import urlsplit

log = logging.getLogger("aiforge.tools.browser.proxy")

_HEAD_CAP = 64 * 1024
_HEAD_TIMEOUT_S = 30.0       # a client that never finishes its head is dropped
_CONNECT_TIMEOUT_S = 10.0
_PIPE_CHUNK = 64 * 1024
# The check and the connect must see the same name: only hostname / IP-literal
# characters, so "allowed.com#.evil.com" cannot parse one way and resolve another.
_HOST_OK = re.compile(r"[a-z0-9.\-:%_]+")
_HOP_HEADERS = ("proxy-connection", "proxy-authorization", "connection",
                "keep-alive")


def _netloc(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def _split_host_port(target: str, default_port: int) -> tuple[str, int]:
    if target.startswith("["):                       # [v6]:port
        host, _, rest = target[1:].partition("]")
        port = rest.lstrip(":")
    else:
        host, _, port = target.rpartition(":") if ":" in target else (target, "", "")
    try:
        return host.lower(), int(port) if port else default_port
    except ValueError:
        return "", 0


# AWS's IPv6 metadata endpoint is a unique-local address, not link-local.
_METADATA_V6 = ipaddress.ip_address("fd00:ec2::254")


def _metadata_like(ip: str) -> bool:
    """Link-local (169.254.169.254 and friends), however it is spelled."""
    try:
        addr = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return True
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped           # ::ffff:169.254.169.254
    return addr.is_link_local or addr == _METADATA_V6


def _resolve(host: str, port: int) -> str | None:
    """First address for ``host``, refusing a name that resolves link-local.

    169.254.169.254 is the cloud metadata service. Whatever the allowlist said
    about the NAME, a name that lands there is refused, and the connection goes
    to the address checked here rather than a second, later lookup.
    """
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return None
    addrs = [i[4][0] for i in infos]
    if not addrs or any(_metadata_like(a) for a in addrs):
        return None
    return addrs[0]


def _read_head(sock: socket.socket) -> tuple[bytes, bytes] | None:
    sock.settimeout(_HEAD_TIMEOUT_S)
    buf = b""
    try:
        while b"\r\n\r\n" not in buf:
            if len(buf) > _HEAD_CAP:
                return None
            chunk = sock.recv(8192)
            if not chunk:
                return None
            buf += chunk
    except OSError:
        return None
    sock.settimeout(None)
    head, _, rest = buf.partition(b"\r\n\r\n")
    return head, rest


def _pipe(a: socket.socket, b: socket.socket, stop: threading.Event) -> None:
    # selectors, not select.select: select fails once a descriptor number
    # passes 1024, which a long-running API server reaches.
    with selectors.DefaultSelector() as sel:
        sel.register(a, selectors.EVENT_READ, b)
        sel.register(b, selectors.EVENT_READ, a)
        while not stop.is_set():
            try:
                ready = sel.select(timeout=1.0)
            except (OSError, ValueError):
                return
            for key, _ in ready:
                src, dst = key.fileobj, key.data
                try:
                    data = src.recv(_PIPE_CHUNK)
                except OSError:
                    return
                if not data:
                    return
                try:
                    dst.sendall(data)
                except OSError:
                    return


def _close(s: socket.socket) -> None:
    # shutdown first: close() from another thread does not wake a thread
    # blocked in recv/send on Linux, so stop() would leave it hanging.
    with contextlib.suppress(OSError):
        s.shutdown(socket.SHUT_RDWR)
    with contextlib.suppress(OSError):
        s.close()


def _header(lines: list[str], name: str) -> str:
    low = name.lower()
    for ln in lines:
        key, sep, value = ln.partition(":")
        if sep and key.strip().lower() == low:
            return value.strip()
    return ""


class EgressProxy:
    """One proxy per browser context. ``allow(host, port)`` decides each
    connection; ``on_block(url)`` is told about every refusal."""

    def __init__(self, allow: Callable[[str, int], bool],
                 on_block: Callable[[str], None]) -> None:
        self._allow = allow
        self._on_block = on_block
        # Per-proxy credentials: without them any process on this machine (a
        # sandboxed shell included) could borrow the BROWSER's egress policy.
        self.username = "aiforge"
        self.password = secrets.token_urlsafe(24)
        self._auth = "Basic " + base64.b64encode(
            f"{self.username}:{self.password}".encode()).decode()
        self._stop = threading.Event()
        self._open: set[socket.socket] = set()
        self._lock = threading.Lock()
        self._server: socketserver.ThreadingTCPServer | None = None
        self.port = 0

    # ── lifecycle ────────────────────────────────────────────────────────
    def start(self) -> int:
        proxy = self

        class _Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                proxy._handle(self.request)

        class _Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = True

        self._server = _Server(("127.0.0.1", 0), _Handler)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True,
                         name=f"browser-egress-proxy-{self.port}").start()
        return self.port

    def stop(self) -> None:
        self._stop.set()
        if self._server is not None:
            with contextlib.suppress(Exception):
                self._server.shutdown()
                self._server.server_close()
        with self._lock:
            socks, self._open = list(self._open), set()
        for s in socks:
            _close(s)

    def _track(self, s: socket.socket) -> None:
        with self._lock:
            self._open.add(s)

    def _untrack(self, s: socket.socket) -> None:
        with self._lock:
            self._open.discard(s)
        _close(s)

    # ── one client connection ────────────────────────────────────────────
    def _handle(self, client: socket.socket) -> None:
        self._track(client)
        upstream = None
        try:
            got = _read_head(client)
            if got is None:
                return
            head, rest = got
            lines = head.decode("latin-1").split("\r\n")
            parts = lines[0].split(" ")
            if len(parts) != 3:
                client.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                return
            method, target, version = parts
            if not hmac.compare_digest(
                    _header(lines[1:], "Proxy-Authorization").encode(),
                    self._auth.encode()):
                client.sendall(b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                               b"Proxy-Authenticate: Basic realm=\"aiforge\"\r\n"
                               b"Content-Length: 0\r\nConnection: close\r\n\r\n")
                return
            if method.upper() == "CONNECT":
                host, port = _split_host_port(target, 443)
                url = f"https://{_netloc(host, port)}/"
                first = b""
            else:
                try:
                    u = urlsplit(target)
                    port = u.port or 80
                except ValueError:            # port 99999, broken brackets
                    u, port = None, 0
                if u is None or u.scheme != "http" or not u.hostname:
                    client.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                    return
                host = u.hostname.lower()
                url = f"http://{_netloc(host, port)}/"
                path = (u.path or "/") + (f"?{u.query}" if u.query else "")
                # One request per upstream connection: a keep-alive client
                # could otherwise send its NEXT request, for another host,
                # down a tunnel that was only checked for this one.
                headers = [ln for ln in lines[1:]
                           if ln.split(":", 1)[0].strip().lower() not in _HOP_HEADERS]
                first = ("\r\n".join([f"{method} {path} {version}", *headers,
                                      "Connection: close"]) + "\r\n\r\n").encode("latin-1")
            ok = bool(host) and _HOST_OK.fullmatch(host) is not None
            ip = _resolve(host, port) if ok and self._allowed(host, port) else None
            if ip is None:
                self._on_block(url)
                client.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n"
                               b"Connection: close\r\n\r\n")
                return
            upstream = socket.create_connection((ip, port), timeout=_CONNECT_TIMEOUT_S)
            upstream.settimeout(None)
            self._track(upstream)
            if first:
                upstream.sendall(first + rest)
            else:
                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                if rest:
                    upstream.sendall(rest)
            _pipe(client, upstream, self._stop)
        except OSError as exc:
            log.debug("egress proxy connection ended: %s", exc)
        finally:
            if upstream is not None:
                self._untrack(upstream)
            self._untrack(client)

    def _allowed(self, host: str, port: int) -> bool:
        try:
            return bool(self._allow(host, port))
        except Exception:  # noqa: BLE001 — fail closed
            return False
