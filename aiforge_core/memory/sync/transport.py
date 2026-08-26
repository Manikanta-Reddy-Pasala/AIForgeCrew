"""Talking to the admin over HTTP. Knows nothing about disk.

An unreachable admin is normal operation, not an error: nothing is queued for
it and nothing blocks on it, so every failure here degrades to "nothing this
cycle" and is retried on the next one. The offer is recomputed from the tree
each cycle, so a missed push needs no outbox.

A *reachable* admin is not trusted either. Everything below is bounded before it
is buffered: the admin is a machine somebody else may administer, and an
unbounded read makes "how much memory does this daemon use" its decision. Bodies
are streamed and abandoned the moment they pass the cap, so a multi-GB blob
costs one chunk, not one allocation of its full size.
"""
from __future__ import annotations

import logging
import threading
import time

from aiforge_core.memory.sync import _deadline

_log = logging.getLogger("aiforge.sync")

# Per *operation* (connect, read, write, pool) — this is what httpx means by
# "timeout", and it is not a request deadline.
TIMEOUT = 20.0

# The deadline for a whole request, enforced by hand while the body streams.
# httpx cannot express it: ``httpx.Timeout(connect=…, read=…)`` bounds the gap
# between two chunks, not the total, so a peer sending one byte every 15 s
# resets the read timer forever and never trips it. Measured before this
# existed: 80 s of one sync cycle spent on 5 dribbled bytes, and a 200-byte
# variant still running past 200 s. The byte caps do not help — at that rate the
# 8 MiB cap is years away. The daemon is sequential, so such a peer stalls every
# other peer *and* the compaction pass behind it. Do not "simplify" this back
# into the timeout argument.
#
# 60 s against the 8 MiB blob cap asks a peer for ~140 KiB/s, which any link
# worth syncing over beats by orders of magnitude, while keeping a handful of
# a sick admin comfortably inside one cycle's budget (see ``loop.CYCLE_BUDGET``).
#
# It bounds the WHOLE request, not just the body. Checking it only inside the
# ``iter_bytes`` loop bounded nothing that mattered: connect, request send and
# *response header* receive all happen before the first chunk exists, and
# TIMEOUT is per read, so a peer dribbling one header byte per second resets
# the read timer forever, never completes its headers, and never lets the loop
# start. Measured at production constants: one such peer held ``run_once`` past
# 90 s with zero rows produced and compaction — which rides the same loop —
# never run.
REQUEST_DEADLINE = 60.0


# A node is a markdown note and a capture is a paste — both kilobytes. 8 MiB is
# three orders of magnitude of headroom for a legitimate blob while still being
# a number the daemon can hold; the applier then hashes the same bytes again, so
# whatever is accepted here is paid for roughly twice.
MAX_BLOB_BYTES = 8 * 1024 * 1024

# A manifest is one small JSON row per advertised entry. The byte cap is lower
# than the blob cap because ``json.loads`` expands: 4 MiB of rows becomes tens
# of MiB of dicts, so the parse is the real cost, not the transfer. The entry
# cap is the same limit expressed in the unit the caller iterates, since a
# pathological body (deeply repeated tiny rows) can stay under the byte cap.
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_MANIFEST_ENTRIES = 20_000


def _q(group: str) -> str:
    """The group as a query-string fragment, or "" when ungrouped.

    Quoted, because the name reaches here from configuration and a value that
    needs escaping must never silently address a different route. The admin
    validates it again anyway — this is about not sending nonsense, not about
    trusting what comes back.
    """
    from urllib.parse import quote

    return f"?group={quote(group, safe='')}" if group else ""


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"} if token else {}


def _token() -> str:
    """The credential this machine presents to the admin, or "".

    ``AIFORGE_SYNC_AUTH=1`` on the admin closes the sync surface and makes it
    demand the ordinary API token — and the docs, the run.sh banner and the
    admin page all name that flag as the way to close an open hub. It was
    documented on the server and unimplemented here: every request sent an empty
    bearer, so an operator who followed the advice got 401 on every offer, push,
    manifest and blob, logged at INFO, and the fleet silently stopped syncing.

    Read per call rather than cached: the env is the configuration, and a daemon
    that has to be restarted to notice a rotated token is a worse failure than
    one extra dict lookup per request.
    """
    import os

    return (os.environ.get("AIFORGE_API_TOKEN") or "").strip()


def _stream(client, url: str, token: str, limit: int, started: float,
            *, method: str = "GET", payload: bytes | None = None) -> bytes:
    """The request itself: connect, send, read headers, stream the body.

    Every phase here can block, which is why it does not run on the caller's
    thread — see ``_fetch``. The in-loop deadline check stays so that a request
    which is merely slow ends itself cleanly, closing the response on the way
    out, rather than being abandoned by the watchdog.

    ``method``/``payload`` carry the spoke's half of the hub protocol. The
    response is streamed and capped identically whichever verb sent it: an
    admin answering an offer is as untrusted as a peer answering a manifest,
    and a POST is not a reason to buffer without a bound.
    """
    total = 0
    chunks: list[bytes] = []
    headers = _headers(token)
    if payload is not None:
        headers["content-type"] = "application/json"
    with client.stream(method, url, headers=headers, content=payload) as r:
        r.raise_for_status()
        declared = 0
        try:
            declared = int(r.headers.get("content-length") or 0)
        except (TypeError, ValueError):
            declared = 0        # a garbage header is simply no header
        if declared > limit:
            raise ValueError(f"peer declares {declared} bytes, cap is {limit}")
        for chunk in r.iter_bytes():
            if time.monotonic() - started > REQUEST_DEADLINE:
                # Same exit as the byte cap: leaving the ``with`` closes the
                # response, so the dribble stops costing us anything.
                raise ValueError(f"response exceeded {REQUEST_DEADLINE:.0f}s deadline")
            total += len(chunk)
            if total > limit:
                # Leaving the ``with`` closes the response, so the rest of the
                # body is never transferred, let alone kept.
                raise ValueError(f"response exceeded {limit} bytes")
            chunks.append(chunk)
    return b"".join(chunks)


def _fetch(url: str, token: str, limit: int, *, method: str = "GET",
           payload: bytes | None = None) -> bytes | None:
    """GET ``url``, refusing anything over ``limit`` bytes. None on any failure.

    Streamed rather than buffered: ``Content-Length`` is a claim, so it is used
    to refuse early when it is honest and the running total is what actually
    enforces the cap when it is not (absent, chunked, or a lie). Bounded in
    wall-clock too, because a slow enough body never reaches any byte cap.

    ``REQUEST_DEADLINE`` is enforced twice, because neither half is sufficient:

    * ``_deadline.client`` bounds every socket operation by the time left, so a
      peer stalling in *any* phase — connect, TLS, response headers, body — is
      hung up on in place. This is what makes the abort real: closing the client
      from another thread does **not** wake a reader already blocked in
      ``recv`` (verified on macOS: the worker stayed blocked indefinitely), so a
      watchdog alone would leak one thread and one socket per hostile request.
    * The worker thread bounds the *caller* whatever httpx does. Name resolution
      happens inside ``connect_tcp`` before any socket exists and honours no
      timeout at all, so a hostile resolver is still unbounded below.
    """
    started = time.monotonic()
    deadline = started + REQUEST_DEADLINE
    client = _deadline.client(TIMEOUT, deadline)
    out: dict = {}

    def _run() -> None:
        try:
            out["body"] = _stream(client, url, token, limit, started,
                                  method=method, payload=payload)
        except BaseException as exc:   # noqa: BLE001 — re-raised on the caller's thread
            out["error"] = exc

    worker = threading.Thread(target=_run, name="aiforge-sync-fetch", daemon=True)
    worker.start()
    worker.join(REQUEST_DEADLINE)
    if worker.is_alive():
        # The socket deadline has already expired, so the worker is either
        # unwinding or stuck somewhere no timeout reaches (DNS). Do not wait for
        # it — waiting is exactly the cost this deadline exists to refuse.
        client.close()
        raise ValueError(f"request exceeded {REQUEST_DEADLINE:.0f}s deadline")
    client.close()
    if "error" in out:
        raise out["error"]
    return out.get("body")


def fetch_groups(base_url: str) -> list[str] | None:
    """What the admin publishes. ``None`` when it could not be reached.

    ``None`` and ``[]`` are different answers and every caller must tell them
    apart: ``[]`` is a reachable ungrouped admin (sync normally), ``None`` is an
    admin that is down (do nothing this cycle, and do not let discovery decide
    anything on the strength of an empty list).
    """
    import json

    from aiforge_core.memory.sync import status

    try:
        raw = _fetch(f"{base_url.rstrip('/')}/api/memory/sync/groups", _token(),
                     MAX_MANIFEST_BYTES)
        data = json.loads(raw)
    except Exception as exc:  # noqa: BLE001 — an unreachable admin is expected
        status.note_failure(base_url, f"{type(exc).__name__}: {exc}"[:200])
        return None
    rows = data.get("groups") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return None
    return [str(g) for g in rows][:MAX_MANIFEST_ENTRIES]


def offer(base_url: str, entries: list[dict], group: str = "") -> list[dict] | None:
    """POST our manifest to the admin; return the entries it wants.

    ``None`` means the admin could not be reached or answered nonsense — the
    caller sends nothing and retries next cycle. An empty list means the admin
    is reachable and already holds everything, which is the steady state and
    must be distinguishable from a failure.
    """
    import json

    from aiforge_core.memory.sync import status

    body = json.dumps({"peer": _self_id(), "group": group,
                       "entries": entries}).encode()
    try:
        raw = _fetch(f"{base_url.rstrip('/')}/api/memory/sync/offer", _token(),
                     MAX_MANIFEST_BYTES, method="POST", payload=body)
        data = json.loads(raw)
    except Exception as exc:  # noqa: BLE001 — an unreachable admin is expected
        status.note_failure(base_url, f"{type(exc).__name__}: {exc}"[:200])
        return None
    want = data.get("want") if isinstance(data, dict) else None
    if not isinstance(want, list):
        return None
    rows = [e for e in want if isinstance(e, dict)]
    if len(rows) > MAX_MANIFEST_ENTRIES:
        # The admin only ever asks for entries we offered it, so a larger list
        # is a broken or hostile admin rather than a big tree. Refused whole:
        # half a push is a sync that silently never completes.
        _log.warning("sync: admin %s asked for %d entries (cap %d) — refused",
                     base_url, len(rows), MAX_MANIFEST_ENTRIES)
        return None
    return rows


def push_blob(base_url: str, entry: dict, body: bytes, group: str = "") -> bool:
    """POST one record the admin asked for. False on any failure.

    The bytes travel base64 inside JSON rather than as a raw body with the entry
    in headers: the entry is a dict with six fields, and a header-encoded
    identity is one more encoding for ``origin``/``key`` to disagree over. The
    cost is the ~33% base64 expansion, which the blob cap already accounts for.
    """
    import base64
    import json

    from aiforge_core.memory.sync import status

    payload = json.dumps({"peer": _self_id(), "group": group, "entry": entry,
                          "body": base64.b64encode(body).decode()}).encode()
    try:
        raw = _fetch(f"{base_url.rstrip('/')}/api/memory/sync/push", _token(),
                     MAX_MANIFEST_BYTES, method="POST", payload=payload)
        data = json.loads(raw)
    except Exception as exc:  # noqa: BLE001 — retried on the next cycle
        status.note_failure(base_url, f"{type(exc).__name__}: {exc}"[:200])
        _log.debug("sync: push of %s to %s failed", entry.get("path"), base_url)
        return False
    return bool(isinstance(data, dict) and data.get("applied"))


def _self_id() -> str:
    """Who we say we are. The admin believes it — see ``inbox`` on why."""
    from aiforge_core.memory.sync import identity

    return identity.self_id()


def fetch_manifest(base_url: str, token: str = "", group: str = "") -> dict:
    """GET the admin's manifest. Returns {} when it is unreachable or absurd.

    ``token`` defaults to whatever this machine is configured with (``_token``);
    pass one explicitly only to override that.
    """
    import json

    from aiforge_core.memory.sync import status

    try:
        body = _fetch(f"{base_url.rstrip('/')}/api/memory/sync/manifest{_q(group)}",
                      token or _token(), MAX_MANIFEST_BYTES)
        data = json.loads(body)
    except Exception as exc:  # noqa: BLE001
        status.note_failure(base_url, f"{type(exc).__name__}: {exc}"[:200])
        return {}
    status.note_success(base_url)
    if not isinstance(data, dict):
        return {}
    for field in ("manifest", "roster"):
        rows = data.get(field)
        if isinstance(rows, list) and len(rows) > MAX_MANIFEST_ENTRIES:
            # Refused whole rather than truncated: half a manifest is a sync
            # that silently never completes, which is harder to notice than a
            # peer that plainly does not sync.
            _log.warning("sync: peer %s advertises %d %s entries (cap %d) — refused",
                         base_url, len(rows), field, MAX_MANIFEST_ENTRIES)
            return {}
        # Coerced here, once, rather than re-checked by every caller: a peer on
        # a different build sent {"manifest": {...}, "roster": "nope"} and the
        # caller's ``roster`` iteration raised *after* the blobs had been
        # applied, and the whole result row vanished from the cycle output. A
        # wrong-shaped field must mean "nothing to sync", not that.
        data[field] = rows if isinstance(rows, list) else []
    return data


def fetch_blob(base_url: str, digest: str, token: str = "",
               group: str = "") -> bytes | None:
    """GET one blob by hash. Returns None on any failure; retried next cycle."""
    from aiforge_core.memory.sync import status

    try:
        return _fetch(
            f"{base_url.rstrip('/')}/api/memory/sync/blob/{digest}{_q(group)}",
            token or _token(), MAX_BLOB_BYTES)
    except Exception as exc:  # noqa: BLE001 — retried on the next cycle
        status.note_failure(base_url, f"{type(exc).__name__}: {exc}"[:200])
        _log.debug("sync: blob %s from %s failed", digest[:8], base_url)
        return None


__all__ = ["fetch_manifest", "fetch_blob", "fetch_groups", "offer",
           "push_blob", "TIMEOUT",
           "REQUEST_DEADLINE", "MAX_BLOB_BYTES", "MAX_MANIFEST_BYTES",
           "MAX_MANIFEST_ENTRIES"]
