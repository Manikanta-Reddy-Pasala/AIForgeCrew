"""An HTTP client whose deadline covers the whole request, not one read.

httpx cannot express this. ``httpx.Timeout(connect=…, read=…)`` bounds the gap
between two socket operations, so a peer that sends one byte per second resets
the read timer forever: measured against a peer dribbling *response headers*,
``fetch_manifest`` never returned at all, because the body loop where the old
hand-rolled deadline lived is not reached until the headers finish.

The fix is to bound the socket rather than the loop. Every read, write and
connect below is given ``min(its own timeout, time left on the deadline)``, so
whichever phase the peer chooses to stall in ends by itself, in place, on the
thread that is stalled — no watchdog can do that, it can only stop *waiting*.

This reaches one attribute deep into httpx (``HTTPTransport._pool``) to install
the backend; everything else it touches is httpcore's public interface. If a
future httpx moves that attribute the client is still returned, just without
the socket-level deadline, and ``transport._fetch``'s watchdog still bounds the
caller — degraded, not broken. Hence the guarded install rather than an assert.
"""
from __future__ import annotations

import logging
import time

import httpcore

_log = logging.getLogger("aiforge.sync")


def _remaining(deadline: float, timeout: float | None) -> float:
    """Time left, never more than the caller's own timeout. Raises when spent."""
    left = deadline - time.monotonic()
    if left <= 0:
        raise httpcore.ReadTimeout("request deadline exceeded")
    return left if timeout is None else min(timeout, left)


class _DeadlineStream(httpcore.NetworkStream):
    """One socket that refuses to outlive the request's deadline."""

    def __init__(self, inner: httpcore.NetworkStream, deadline: float) -> None:
        self._inner = inner
        self._deadline = deadline

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return self._inner.read(max_bytes, _remaining(self._deadline, timeout))

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self._inner.write(buffer, _remaining(self._deadline, timeout))

    def close(self) -> None:
        self._inner.close()

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        # The handshake is several round trips, each of which a hostile peer can
        # stall exactly like a body — so the wrapper has to survive the upgrade.
        return _DeadlineStream(
            self._inner.start_tls(ssl_context, server_hostname,
                                  _remaining(self._deadline, timeout)),
            self._deadline)

    def get_extra_info(self, info: str):
        return self._inner.get_extra_info(info)


class _DeadlineBackend(httpcore.NetworkBackend):
    """Hands out sockets that already know when the request must be over."""

    def __init__(self, deadline: float) -> None:
        self._inner = httpcore.SyncBackend()
        self._deadline = deadline

    def connect_tcp(self, host, port, timeout=None, local_address=None,
                    socket_options=None):
        return _DeadlineStream(
            self._inner.connect_tcp(host, port,
                                    _remaining(self._deadline, timeout),
                                    local_address, socket_options),
            self._deadline)

    def connect_unix_socket(self, path, timeout=None, socket_options=None):
        return _DeadlineStream(
            self._inner.connect_unix_socket(path,
                                            _remaining(self._deadline, timeout),
                                            socket_options),
            self._deadline)


def client(timeout: float, deadline: float):
    """An ``httpx.Client`` bounded by ``deadline`` (a ``time.monotonic`` stamp)."""
    import httpx

    http = httpx.HTTPTransport()
    try:
        http._pool._network_backend = _DeadlineBackend(deadline)
    except AttributeError as exc:      # pragma: no cover — a future httpx layout
        _log.warning("sync: request deadline not installed at the socket (%s); "
                     "falling back to the caller-side watchdog only", exc)
    return httpx.Client(timeout=timeout, transport=http)


__all__ = ["client"]
