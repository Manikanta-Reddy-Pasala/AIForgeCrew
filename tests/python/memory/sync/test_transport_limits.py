"""A peer is somebody else's machine: nothing it sends may be read unbounded.

Each test drives the real client against a real (hostile) local HTTP server, so
it constrains the behaviour on the wire rather than the shape of the code: the
server counts the bytes it managed to push, which is what "without buffering"
actually means.
"""
from __future__ import annotations

import base64
import contextlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

CHUNK = b"x" * 65536
FLOOD = 64 * 1024 * 1024      # what a hostile peer would like us to swallow


def _serve(handler):
    """Start a one-route HTTP server; yields ``(base_url, state)``."""
    state = {"sent": 0, "hits": 0}

    class _H(BaseHTTPRequestHandler):
        def log_message(self, *_args):     # keep pytest output readable
            pass

        def do_GET(self):                  # noqa: N802 — BaseHTTPRequestHandler API
            state["hits"] += 1
            # The client hanging up mid-flood is the point of these tests.
            with contextlib.suppress(Exception):
                handler(self, state)

        # The push half of the protocol is POST, and it is bounded by the same
        # caps — so it is driven against the same hostile server.
        do_POST = do_GET               # noqa: N815 — mirrors the API above

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}", state


@pytest.fixture
def peer():
    started = []

    def _start(handler):
        srv, base, state = _serve(handler)
        started.append(srv)
        return base, state

    yield _start
    for srv in started:
        srv.shutdown()
        srv.server_close()


def _flood(h, state, declare: int = 0):
    h.send_response(200)
    h.send_header("Content-Type", "application/octet-stream")
    if declare:
        h.send_header("Content-Length", str(declare))
    h.end_headers()
    while state["sent"] < FLOOD:
        h.wfile.write(CHUNK)
        h.wfile.flush()
        state["sent"] += len(CHUNK)


def _json_body(h, payload: bytes):
    h.send_response(200)
    h.send_header("Content-Type", "application/json")
    h.send_header("Content-Length", str(len(payload)))
    h.end_headers()
    h.wfile.write(payload)


def test_an_oversized_content_length_is_refused_before_the_body_is_read(peer):
    """The header is a claim, but an honest oversized one must cost nothing."""
    from aiforge_core.memory.sync import transport

    base, state = peer(lambda h, s: _flood(h, s, declare=FLOOD))

    assert transport.fetch_blob(base, "deadbeef", "t") is None
    time.sleep(0.2)                        # let the server finish blocking
    assert state["sent"] < transport.MAX_BLOB_BYTES, (
        f"server pushed {state['sent']} bytes of a {FLOOD}-byte body we refused")


def test_an_oversized_streamed_body_aborts_at_the_cap(peer):
    """No Content-Length at all: only the running total can stop it."""
    from aiforge_core.memory.sync import transport

    base, state = peer(_flood)

    assert transport.fetch_blob(base, "deadbeef", "t") is None
    time.sleep(0.2)
    assert state["sent"] < FLOOD // 2, (
        f"server pushed {state['sent']} bytes; the cap is "
        f"{transport.MAX_BLOB_BYTES}")


def test_an_oversized_manifest_body_is_refused(peer):
    from aiforge_core.memory.sync import transport

    base, state = peer(lambda h, s: _flood(h, s, declare=FLOOD))

    assert transport.fetch_manifest(base, "t") == {}
    time.sleep(0.2)
    assert state["sent"] < transport.MAX_MANIFEST_BYTES


def test_a_manifest_with_absurdly_many_entries_is_refused(peer):
    """Small rows stay under the byte cap; the entry cap is the one that bites."""
    from aiforge_core.memory.sync import transport

    rows = [{"k": i} for i in range(transport.MAX_MANIFEST_ENTRIES + 1)]
    payload = json.dumps({"manifest": rows}).encode()
    assert len(payload) < transport.MAX_MANIFEST_BYTES
    base, _state = peer(lambda h, _s: _json_body(h, payload))

    assert transport.fetch_manifest(base, "t") == {}


def test_an_ordinary_peer_still_syncs(peer):
    """The caps must not cost the normal case anything."""
    from aiforge_core.memory.sync import transport

    body = json.dumps({"manifest": [{"key": "O-01", "hash": "ab"}],
                       "roster": [{"id": "nuc"}]}).encode()
    base, _state = peer(lambda h, _s: _json_body(h, body))

    assert transport.fetch_manifest(base, "t")["manifest"][0]["key"] == "O-01"


def test_offer_posts_our_manifest_and_returns_what_the_admin_wants(peer):
    """The push half of the protocol: a POST body the admin can read back, and
    a wanted-list that is refused rather than truncated when it is absurd."""
    from aiforge_core.memory.sync import transport

    seen = {}

    def _offer(h, _s):
        length = int(h.headers.get("Content-Length") or 0)
        seen["method"] = h.command
        seen["body"] = json.loads(h.rfile.read(length))
        _json_body(h, json.dumps({"want": [{"hash": "ab", "kind": "A"}]}).encode())

    base, _state = peer(_offer)
    want = transport.offer(base, [{"hash": "ab", "kind": "A", "path": "captures/a.md"}])

    assert want == [{"hash": "ab", "kind": "A"}]
    assert seen["method"] == "POST"
    assert seen["body"]["entries"][0]["path"] == "captures/a.md"
    assert seen["body"]["peer"]                      # we say who we are

    # An admin that answers with something else means "send nothing", not
    # "send everything" — None is distinguishable from an empty want list.
    def _junk(h, _s):
        _json_body(h, b'{"want": "nope"}')

    base2, _s2 = peer(_junk)
    assert transport.offer(base2, []) is None


def test_push_blob_reports_whether_the_admin_applied_it(peer):
    from aiforge_core.memory.sync import transport

    seen = {}

    def _push(h, _s):
        length = int(h.headers.get("Content-Length") or 0)
        seen["body"] = json.loads(h.rfile.read(length))
        _json_body(h, b'{"applied": true}')

    base, _state = peer(_push)
    entry = {"hash": "ab", "kind": "A", "path": "captures/a.md"}
    assert transport.push_blob(base, entry, b"hello") is True
    assert base64.b64decode(seen["body"]["body"]) == b"hello"

    # A refusal is a normal answer, not an exception: the record is simply
    # offered again next cycle.
    def _refused(h, _s):
        _json_body(h, b'{"applied": false}')

    base2, _s2 = peer(_refused)
    assert transport.push_blob(base2, entry, b"hello") is False


def test_a_blob_within_the_cap_arrives_whole(peer):
    from aiforge_core.memory.sync import transport

    payload = b"y" * (256 * 1024)

    def _ok(h, _s):
        h.send_response(200)
        h.send_header("Content-Length", str(len(payload)))
        h.end_headers()
        h.wfile.write(payload)

    base, _state = peer(_ok)
    assert transport.fetch_blob(base, "ab", "t") == payload
