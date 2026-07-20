"""A peer that is slow rather than absent, and the cycle it would otherwise eat.

A dead peer costs a timeout and is cheap. A *dribbling* peer — headers, then a
byte at a time — costs nothing per operation and everything in total, because
httpx's timeout is per read, not per request. The daemon is sequential, so one
of these stops every other peer and the compaction pass behind it. Driven
against a real socket: this is wire behaviour, not code shape.
"""
from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


def _serve(handler):
    class _H(BaseHTTPRequestHandler):
        def log_message(self, *_args):     # keep pytest output readable
            pass

        def do_GET(self):                  # noqa: N802 — BaseHTTPRequestHandler API
            try:
                handler(self)
            except Exception:              # noqa: BLE001 — we hang up on purpose
                pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


@pytest.fixture
def peer():
    started = []

    def _start(handler):
        srv, base = _serve(handler)
        started.append(srv)
        return base

    yield _start
    for srv in started:
        srv.shutdown()
        srv.server_close()


def _dribble(h):
    """Headers, then one byte every 0.25 s — never idle long enough to time out.

    Bounded at 40 bytes (~10 s) only so that a regression fails the suite
    instead of hanging it; a real hostile peer simply never stops.
    """
    h.send_response(200)
    h.send_header("Content-Type", "application/octet-stream")
    h.end_headers()
    for _ in range(40):
        h.wfile.write(b"x")
        h.wfile.flush()
        time.sleep(0.25)


def test_a_dribbling_peer_is_abandoned_at_the_request_deadline(peer, monkeypatch):
    """Measured before the deadline existed: 80 s of one cycle for 5 bytes, and
    a longer variant still blocked past 200 s. No byte cap can help — at this
    rate the 8 MiB cap is years away."""
    from aiforge_core.memory.sync import transport

    monkeypatch.setattr(transport, "REQUEST_DEADLINE", 2.0)
    base = peer(_dribble)

    started = time.monotonic()
    assert transport.fetch_blob(base, "deadbeef", "t") is None
    elapsed = time.monotonic() - started
    assert elapsed < 7.0, f"dribbling peer held the cycle for {elapsed:.1f}s"


def test_the_default_deadline_bounds_a_request_well_inside_one_cycle():
    """The constant is the guard; a value near the interval would be no guard."""
    from aiforge_core.memory.sync import loop, transport

    assert transport.REQUEST_DEADLINE <= loop.CYCLE_BUDGET
    assert loop.CYCLE_BUDGET < loop.DEFAULT_INTERVAL


def test_a_cycle_of_sick_peers_stays_inside_its_budget_and_reports_the_skipped(
        monkeypatch, tmp_path):
    """Per-request deadlines bound one peer, never their sum: with MAX_PEERS=64
    a handful of these ran a cycle past the interval, and the peers that never
    got their turn were simply absent from the output."""
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_PEER_ID", "book")
    from aiforge_core.memory.sync import loop, peers, transport

    peers.save({"self": {"id": "book", "urls": []},
                "peers": [{"id": f"p{i}", "urls": [f"http://stub/{i}"],
                           "state": peers.STATE_APPROVED} for i in range(6)]})

    def _slow(*_a, **_k):
        time.sleep(0.4)
        return {}

    monkeypatch.setattr(transport, "fetch_manifest", _slow)
    monkeypatch.setattr(loop, "CYCLE_BUDGET", 1.0)

    started = time.monotonic()
    rows = loop.run_once()
    elapsed = time.monotonic() - started

    assert elapsed < 2.0, f"cycle ran {elapsed:.1f}s on a 1s budget"
    assert len(rows) == 6, "peers dropped for budget must still be reported"
    assert [r["peer"] for r in rows] == [f"p{i}" for i in range(6)]
    assert any(r.get("skipped") for r in rows)
    assert not rows[0].get("skipped")
