"""A peer that is slow rather than absent, and the cycle it would otherwise eat.

A dead peer costs a timeout and is cheap. A *dribbling* peer — a byte at a time
— costs nothing per operation and everything in total, because httpx's timeout
is per read, not per request. The daemon is sequential, so one of these stops
every other peer and the compaction pass behind it.

Driven against a raw socket, not ``BaseHTTPRequestHandler``: that class always
completes its headers, so a test built on it can only ever reach the body loop.
The phase that actually went unbounded in production is the one *before* the
body — connect, request, response headers — where no amount of checking inside
``iter_bytes`` can help, because ``iter_bytes`` has not been reached. A server
that cannot dribble headers cannot test the bug.
"""
from __future__ import annotations

import socket
import threading
import time

import pytest

_WORKER = "aiforge-sync-fetch"


def _raw_server(handler):
    """A listener that hands each connection to ``handler`` raw. No HTTP stack."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)

    def _accept():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:            # the fixture closed the listener
                return
            threading.Thread(target=handler, args=(conn,), daemon=True).start()

    threading.Thread(target=_accept, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.getsockname()[1]}"


@pytest.fixture
def raw_peer():
    started = []

    def _start(handler):
        srv, base = _raw_server(handler)
        started.append(srv)
        return base

    yield _start
    for srv in started:
        srv.close()


def _dribble_headers(conn):
    """A valid status line, then header bytes forever — never ``\\r\\n\\r\\n``.

    One byte every 0.25 s, so the per-read timer is reset long before it fires
    and the response headers never complete. Bounded at 15 s only so that a
    regression fails the suite instead of hanging it; a real hostile peer just
    never stops.
    """
    try:
        conn.recv(65535)
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nX-Pad: ")
        for _ in range(60):
            conn.sendall(b"a")
            time.sleep(0.25)
    except OSError:                    # we were hung up on, which is the point
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


def _silent(conn):
    """Accept the connection and say nothing at all."""
    try:
        conn.recv(65535)
        time.sleep(30)
    except OSError:
        pass


@pytest.mark.parametrize("call", ["manifest", "blob"])
def test_a_peer_dribbling_response_headers_is_abandoned_at_the_deadline(
        raw_peer, monkeypatch, call):
    """The deadline used to be checked only inside ``for chunk in iter_bytes()``,
    which a peer that never finishes its headers never reaches. Measured at
    production constants: ``fetch_manifest`` against this server did not return
    at all — still blocked when the harness hard-exited it."""
    from aiforge_core.memory.sync import transport

    monkeypatch.setattr(transport, "REQUEST_DEADLINE", 2.0)
    base = raw_peer(_dribble_headers)

    started = time.monotonic()
    if call == "manifest":
        assert transport.fetch_manifest(base, "t") == {}
    else:
        assert transport.fetch_blob(base, "deadbeef", "t") is None
    elapsed = time.monotonic() - started

    assert elapsed < 5.0, f"header-dribbling peer held the cycle for {elapsed:.1f}s"


def test_a_peer_that_answers_nothing_is_abandoned_at_the_deadline(
        raw_peer, monkeypatch):
    """Connect succeeds, then silence: bounded by the deadline, not by TIMEOUT,
    so shrinking the deadline below TIMEOUT actually shortens the wait."""
    from aiforge_core.memory.sync import transport

    monkeypatch.setattr(transport, "REQUEST_DEADLINE", 1.0)
    monkeypatch.setattr(transport, "TIMEOUT", 20.0)
    base = raw_peer(_silent)

    started = time.monotonic()
    assert transport.fetch_manifest(base, "t") == {}
    assert time.monotonic() - started < 4.0


def test_an_abandoned_request_does_not_leak_its_worker(raw_peer, monkeypatch):
    """The deadline is enforced at the socket, not only by the waiting caller.

    Closing the client from another thread does *not* wake a reader already
    blocked in ``recv`` — verified on macOS, where the worker stayed blocked
    indefinitely. A caller-side watchdog alone would therefore leak one thread
    and one socket per hostile request, and MAX_PEERS is 64.
    """
    from aiforge_core.memory.sync import transport

    monkeypatch.setattr(transport, "REQUEST_DEADLINE", 1.0)
    base = raw_peer(_dribble_headers)

    for _ in range(3):
        transport.fetch_manifest(base, "t")

    limit = time.monotonic() + 5.0
    while time.monotonic() < limit:
        if not [t for t in threading.enumerate() if t.name == _WORKER]:
            break
        time.sleep(0.1)

    assert [t for t in threading.enumerate() if t.name == _WORKER] == []


def test_the_default_deadline_bounds_a_request_well_inside_one_cycle():
    """The constant is the guard; a value near the interval would be no guard."""
    from aiforge_core.memory.sync import loop, transport

    assert transport.REQUEST_DEADLINE <= loop.CYCLE_BUDGET
    assert loop.CYCLE_BUDGET < loop.DEFAULT_INTERVAL
