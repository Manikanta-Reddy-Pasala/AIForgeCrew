"""Operator admin surface for P2P memory sync (/admin + /api/admin/*).

Read-only and loopback-only. There is no new auth system here: the gate is
"the request came from this machine", which is the same trust boundary the
operator already crossed to run the process. Anything further away is told to
use an SSH tunnel.

This module is a thin adapter over ``memory.sync`` — ``peers``, ``lease`` and
``manifest`` own the data, this file only shapes it for a screen.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

router = APIRouter()

_af_log = logging.getLogger("aiforge")

# The only addresses that mean "this machine". IPv6 loopback and the
# v4-mapped-in-v6 form appear depending on how the socket was bound.
_LOOPBACK = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}

# A peer that is down is the common case, so the probe must fail fast: this
# runs inline in a page load. transport.fetch_manifest is deliberately NOT
# reused — its 20s TIMEOUT is right for the sync loop (which has all cycle to
# wait) and wrong for a UI, and it swallows the exception, so there would be
# no error string and no latency to show. Changing transport's timeout would
# degrade the sync loop to serve this page, so httpx is used directly instead.
PROBE_TIMEOUT = 3.0


def _require_loopback(request: Request) -> None:
    """Admin is loopback-only. 403 for anything else.

    The decision is made ONLY from ``request.client.host`` — the peer address
    of the actual TCP connection. X-Forwarded-For, X-Real-IP and Host are all
    set by the client and can say anything; trusting them is the standard way
    a "localhost only" gate gets bypassed by one extra header. There is no
    trusted proxy in front of this app, so there is nothing to consult them for.
    """
    client = getattr(request, "client", None)
    host = getattr(client, "host", None) if client else None
    if host not in _LOOPBACK:
        # Not a secret — an operator debugging from their laptop deserves to be
        # told why, and how to get in, rather than a blank 403.
        raise HTTPException(
            403,
            f"admin is loopback-only (client {host or 'unknown'}); reach it over an "
            "SSH tunnel, e.g. ssh -L 8799:127.0.0.1:8799 <host> then open "
            "http://127.0.0.1:8799/admin",
        )


def _probe_peer(peer: dict) -> dict:
    """Live reachability for one peer. Never raises — down is normal."""
    import time

    import httpx

    urls = [u for u in (peer.get("urls") or []) if u]
    if not urls:
        return {"reachable": False, "latency_ms": None, "their_entries": None,
                "error": "no url configured"}
    token = str(peer.get("token") or "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    started = time.monotonic()
    try:
        r = httpx.get(f"{urls[0].rstrip('/')}/api/memory/sync/manifest",
                      headers=headers, timeout=PROBE_TIMEOUT)
        r.raise_for_status()
        entries = (r.json() or {}).get("manifest") or []
        return {"reachable": True,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "their_entries": len(entries), "error": None}
    except Exception as exc:  # noqa: BLE001 — an unreachable peer is a datum, not a failure
        return {"reachable": False,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "their_entries": None, "error": f"{type(exc).__name__}: {exc}"[:200]}


def _probe_all(peers: list[dict]) -> list[dict]:
    """Probe every peer concurrently so one dead peer costs one timeout, not N."""
    from concurrent.futures import ThreadPoolExecutor

    if not peers:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(peers))) as pool:
        return list(pool.map(_probe_peer, peers))


def _local_counts() -> dict:
    from aiforge_core.memory.sync import manifest as _man
    from aiforge_core.memory.sync import paths as _paths

    entries = _man.build()
    class_a = sum(1 for e in entries if e.get("kind") == "A")
    tombs = sum(1 for _ in _paths.tomb_dir().rglob("*.json")) if _paths.tomb_dir().exists() else 0
    okf = _paths.okf_dir()
    conflicts = sum(1 for _ in okf.rglob("*.conflict.md")) if okf.exists() else 0
    return {"class_a": class_a, "class_b": len(entries) - class_a,
            "tombstones": tombs, "conflicts": conflicts, "total": len(entries)}


def _lease_view() -> dict:
    import time

    from aiforge_core.memory.sync import lease as _lease
    from aiforge_core.memory.sync import merge as _merge

    rec = _lease.read() or {}
    expires_at = _merge.as_rev(rec.get("expires_at"))
    remaining = expires_at - int(time.time()) if expires_at else 0
    return {"holder": rec.get("holder") or None,
            "is_holder": bool(_lease.is_holder()),
            "expires_in": max(0, remaining) if rec else None,
            "rev": _merge.as_rev(rec.get("rev"))}


@router.get("/api/admin/sync-status")
def sync_status(request: Request, probe: int = Query(1)) -> dict:
    _require_loopback(request)
    from aiforge_core.memory.sync import identity as _identity
    from aiforge_core.memory.sync import peers as _peers

    data = _peers.load()
    raw = list(data.get("peers") or [])
    probes = _probe_all(raw) if probe else [{} for _ in raw]

    out = []
    for p, pr in zip(raw, probes, strict=False):
        # NOTE: fields are listed explicitly — p may carry a token and this
        # response must never echo one.
        out.append({"id": str(p.get("id") or ""), "state": str(p.get("state") or ""),
                    "urls": [str(u) for u in (p.get("urls") or [])],
                    "last_seen": int(p.get("last_seen") or 0),
                    "reachable": pr.get("reachable", False),
                    "latency_ms": pr.get("latency_ms"),
                    "their_entries": pr.get("their_entries"),
                    "error": pr.get("error")})

    me = data.get("self") or {}
    return {"self": {"id": _identity.self_id(),
                     "urls": [str(u) for u in (me.get("urls") or [])]},
            "lease": _lease_view(),
            "local": _local_counts(),
            "peers": out,
            "probed": bool(probe)}


@router.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request) -> HTMLResponse:
    # Gated identically to the JSON: a page that refuses to render while its
    # data endpoint answers the whole LAN would only LOOK protected.
    _require_loopback(request)
    import json

    # Boot payload is the UNPROBED status: the page paints real local state
    # immediately with no network wait, then the first fetch below fills in
    # peer reachability. Same shaping function, so it carries no tokens either.
    # "<" is escaped so a peer id or url can never close the <script> block.
    boot = json.dumps(sync_status(request, probe=0)).replace("<", "\\u003c")
    return HTMLResponse(_PAGE.replace("__BOOT__", boot))


_PAGE = """<title>AIForge · sync status</title>
<style>
:root{color-scheme:light dark;--bg:#fbfbfa;--fg:#1a1a19;--dim:#6b6b66;--line:#e3e2de;
--card:#fff;--ok:#0f7b46;--warn:#8a5a00;--bad:#a3242b;--okbg:#e6f4ec;--warnbg:#fdf1dc;
--badbg:#fbe9e9}
@media (prefers-color-scheme:dark){:root{--bg:#131315;--fg:#ececea;--dim:#9a9a95;
--line:#2c2c30;--card:#1c1c1f;--ok:#5cd394;--warn:#e0b25f;--bad:#f08b8f;
--okbg:#15301f;--warnbg:#33280f;--badbg:#3a1a1c}}
body{margin:0;padding:24px;background:var(--bg);color:var(--fg);
font:14px/1.5 ui-sans-serif,-apple-system,Segoe UI,Roboto,sans-serif}
h1{font-size:17px;margin:0 0 2px}
.sub{color:var(--dim);font-size:12px;margin-bottom:18px}
.cards{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:18px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;
padding:10px 14px;min-width:110px}
.card b{display:block;font-size:19px;font-variant-numeric:tabular-nums;font-weight:600}
.card span{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.05em}
table{width:100%;border-collapse:collapse;background:var(--card);
border:1px solid var(--line);border-radius:8px;overflow:hidden}
th{text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.05em;
color:var(--dim);padding:8px 10px;border-bottom:1px solid var(--line)}
td{padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
tr:last-child td{border-bottom:0}
.num{font-variant-numeric:tabular-nums;text-align:right}
.pill{display:inline-block;padding:1px 7px;border-radius:999px;font-size:11px;
font-weight:600;border:1px solid transparent}
.pill.approved{background:var(--okbg);color:var(--ok);border-color:var(--ok)}
.pill.candidate{background:var(--warnbg);color:var(--warn);border-style:dashed;
border-color:var(--warn)}
.dot{display:inline-block;width:9px;height:9px;margin-right:5px;background:var(--ok);
border-radius:50%}
.dot.down{background:var(--bad);border-radius:1px;transform:rotate(45deg)}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
.dim{color:var(--dim)}
.bad{color:var(--bad)}
.note{background:var(--warnbg);color:var(--warn);border:1px dashed var(--warn);
border-radius:6px;padding:8px 10px;font-size:12px;margin-top:12px}
button{font:inherit;background:var(--card);color:var(--fg);border:1px solid var(--line);
border-radius:6px;padding:3px 10px;cursor:pointer}
</style>
<h1>Memory sync status</h1>
<div class="sub"><span id="age">loading…</span> · <button id="refresh">Refresh</button></div>
<div id="err" class="note" style="display:none"></div>
<div class="cards" id="cards"></div>
<table><thead><tr><th>Peer</th><th>State</th><th>Reachable</th><th class="num">Latency</th>
<th class="num">Entries</th><th class="num">Last seen</th><th>URLs</th></tr></thead>
<tbody id="rows"></tbody></table>
<div id="cand" class="note" style="display:none">Candidate peers were
<b>discovered, not trusted</b>. They are never pulled from. Approve one by adding its
token to <span class="mono">peers.json</span> and setting its state to
<span class="mono">approved</span>.</div>
<script>
var last = 0;
function ago(s){ if(!s) return '—';
  var d = Math.max(0, Math.floor(Date.now()/1000) - s);
  if (d < 60) return d + 's'; if (d < 3600) return Math.floor(d/60) + 'm';
  if (d < 86400) return Math.floor(d/3600) + 'h'; return Math.floor(d/86400) + 'd'; }
function esc(v){ return String(v == null ? '' : v)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function card(l, v){ return '<div class="card"><b>' + esc(v) + '</b><span>' + l + '</span></div>'; }
function render(d){
  var L = d.lease, C = d.local;
  document.getElementById('cards').innerHTML =
    card('this peer', d.self.id) +
    card('lease holder', L.holder ? (L.is_holder ? L.holder + ' (us)' : L.holder) : 'none') +
    card('class A', C.class_a) + card('class B', C.class_b) +
    card('tombstones', C.tombstones) + card('conflicts', C.conflicts) +
    card('peers', d.peers.length);
  document.getElementById('rows').innerHTML = d.peers.map(function(p){
    var reach = d.probed
      ? (p.reachable ? '<span class="dot"></span>up'
                     : '<span class="dot down"></span><span class="bad">down</span>'
                       + (p.error ? ' <span class="dim mono">' + esc(p.error) + '</span>' : ''))
      : '<span class="dim">not probed</span>';
    return '<tr><td class="mono">' + esc(p.id) + '</td>'
      + '<td><span class="pill ' + esc(p.state) + '">' + esc(p.state) + '</span></td>'
      + '<td>' + reach + '</td>'
      + '<td class="num">' + (p.latency_ms == null ? '—' : p.latency_ms + ' ms') + '</td>'
      + '<td class="num">' + (p.their_entries == null ? '—' : p.their_entries) + '</td>'
      + '<td class="num">' + ago(p.last_seen) + '</td>'
      + '<td class="mono dim">' + esc((p.urls || []).join(' ')) + '</td></tr>';
  }).join('') || '<tr><td colspan="7" class="dim">No peers configured.</td></tr>';
  document.getElementById('cand').style.display =
    d.peers.some(function(p){ return p.state === 'candidate'; }) ? 'block' : 'none';
}
function load(){
  fetch('/api/admin/sync-status').then(function(r){
    if (!r.ok) throw new Error('HTTP ' + r.status); return r.json();
  }).then(function(d){
    document.getElementById('err').style.display = 'none';
    last = Date.now(); render(d); tick();
  }).catch(function(e){
    var el = document.getElementById('err');
    el.style.display = 'block';
    el.textContent = 'Could not load sync status: ' + e.message
      + ' — is the API still running?';
  });
}
function tick(){ document.getElementById('age').textContent = last
  ? 'updated ' + Math.round((Date.now() - last)/1000) + 's ago' : 'never updated'; }
document.getElementById('refresh').onclick = load;
render(__BOOT__);        // local state, painted with no network round-trip
setInterval(load, 10000); setInterval(tick, 1000); load();
</script>
"""

__all__ = ["router"]
