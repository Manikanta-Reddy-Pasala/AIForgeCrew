"""Operator admin surface for hub memory sync (/admin + /api/admin/*).

Read-only, and reachable on **loopback OR a valid token** — the same rule
``api._require_token`` applies to everything else; this surface is not special
-cased. On top of that, the ``_require_loopback`` dependency below refuses a
remote caller *even with a valid token*, so a stolen token does not open the
admin page from another machine.

An earlier revision demanded the token here regardless of peer address, on the
reasoning that the highest-value surface should not rest on the weakest signal.
It was reverted (``11f5778``): a browser navigation cannot send an
``Authorization`` header, so the day a token existed this page stopped opening
in a browser at all.

The weak signal is real: a same-host reverse proxy makes every request on earth
arrive from ``127.0.0.1``. **``AIFORGE_TRUST_LOOPBACK=0`` is the only thing
that closes that**, and a fronted deployment must set it for the rest of the
API regardless. Do not re-add a special case here instead — it charges every
correctly-configured operator for a hole that flag already shuts.

This module is a thin adapter over ``memory.sync`` — ``role``, ``inbox`` and
``manifest`` own the data, this file only shapes it for a screen.
"""
from __future__ import annotations

import logging

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

router = APIRouter()

_af_log = logging.getLogger("aiforge")

# The only addresses that mean "this machine". IPv6 loopback and the
# v4-mapped-in-v6 form appear depending on how the socket was bound.
_LOOPBACK = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}

# An admin that is down is the common case, so the probe must fail fast: this
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


def _probe_admin(base_url: str) -> dict:
    """Live reachability of the one machine we sync with. Never raises.

    One fixed address, taken from this machine's own configuration — not a
    roster somebody else can grow — so there is no fan-out to bound and no
    request-forgery surface to worry about the way the old peer table had.
    """
    import time

    import httpx

    if not base_url:
        return {"reachable": False, "latency_ms": None, "entries": None,
                "error": None, "probed": False}
    from aiforge_core.memory.sync import transport as _transport

    started = time.monotonic()
    try:
        # Presents the same credential the sync loop does. Without it, an
        # operator who closes the surface with AIFORGE_SYNC_AUTH=1 — which this
        # very page tells them to do — sees a perfectly healthy admin reported
        # as "down: HTTPStatusError 401" forever, indistinguishable from a real
        # outage.
        r = httpx.get(f"{base_url.rstrip('/')}/api/memory/sync/manifest",
                      headers=_transport._headers(_transport._token()),
                      timeout=PROBE_TIMEOUT)
        r.raise_for_status()
        data = r.json() or {}
        return {"reachable": True,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "entries": len(data.get("manifest") or []),
                "id": str(data.get("admin") or ""),
                "error": None, "probed": True}
    except Exception as exc:  # noqa: BLE001
        return {"reachable": False,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "entries": None,
                "error": f"{type(exc).__name__}: {exc}"[:200], "probed": True}


def _md_count(directory) -> int:
    return sum(1 for _ in directory.rglob("*.md")) if directory.is_dir() else 0


def _local_counts() -> dict:
    """What this node holds, and — since each of the four directories has a
    different writer — where it holds it."""
    from aiforge_core.memory.sync import manifest as _man
    from aiforge_core.memory.sync import paths as _paths

    entries = _man.build()
    class_a = sum(1 for e in entries if e.get("kind") == "A")
    tombs = sum(1 for _ in _paths.tomb_dir().rglob("*.json")) if _paths.tomb_dir().exists() else 0
    okf = _paths.okf_dir()
    conflicts = sum(1 for _ in okf.rglob("*.conflict.md")) if okf.exists() else 0
    return {"class_a": class_a, "class_b": len(entries) - class_a,
            "tombstones": tombs, "conflicts": conflicts, "total": len(entries),
            "okf": _md_count(okf), "peers": _md_count(_paths.peers_root()),
            "mesh": _md_count(_paths.mesh_dir()), "view": _md_count(_paths.view_dir())}


def _role_view() -> dict:
    """Which machine runs the cross-machine merge, and whether that is us.

    Configuration, not an election: ``role`` reads ``AIFORGE_ADMIN_URL`` /
    ``AIFORGE_ROLE`` and answers immediately, so there is nothing to probe and
    nothing to age out (see ``memory.sync.role``).
    """
    from aiforge_core.memory.sync import identity as _identity
    from aiforge_core.memory.sync import role as _role

    is_admin = _role.is_admin()
    url = _role.admin_url()
    return {"role": _role.role(), "is_admin": is_admin,
            "admin_url": url, "admin_id": _role.admin_id(),
            "self": _identity.self_id(),
            # A spoke with no admin named neither syncs nor merges, and used to
            # render as "merges only its own knowledge" — the opposite of what
            # happens. Surfaced as its own state rather than inferred in the
            # template, so the JSON says it too.
            "stranded": bool(not is_admin and not url),
            # An admin that still carries a url is a misconfiguration the page
            # would otherwise hide behind "admin: this machine".
            "stale_url": url if (is_admin and url) else ""}


@router.get("/api/admin/sync-status")
def sync_status(request: Request, probe: Annotated[int, Query()] = 1) -> dict:
    _require_loopback(request)
    from aiforge_core.memory.sync import inbox as _inbox

    role = _role_view()
    admin = (_probe_admin(role["admin_url"]) if probe and not role["is_admin"]
             else {"reachable": False, "latency_ms": None, "entries": None,
                   "error": None, "probed": False})
    return {"self": {"id": role["self"]},
            "role": role,
            "local": _local_counts(),
            # Only meaningful on the admin — a spoke has an empty roll, which
            # the page renders as the "you are a spoke" case rather than as
            # "nobody is syncing".
            "spokes": _inbox.roll() if role["is_admin"] else [],
            "admin": admin,
            "probed": bool(probe)}


@router.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request) -> HTMLResponse:
    # Gated identically to the JSON: a page that refuses to render while its
    # data endpoint answers the whole LAN would only LOOK protected.
    _require_loopback(request)
    import json

    # Boot payload is the UNPROBED status: the page paints real local state
    # immediately with no network wait, then the first fetch below fills in
    # reachability. "<" is escaped so an id or url can never close the <script>.
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
font-weight:600;border:1px solid var(--ok);background:var(--okbg);color:var(--ok)}
.pill.spoke{border-color:var(--warn);background:var(--warnbg);color:var(--warn)}
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
<table><thead><tr><th id="th1">Machine</th><th>State</th><th class="num">Latency</th>
<th class="num">Entries</th><th class="num">Last seen</th></tr></thead>
<tbody id="rows"></tbody></table>
<div id="hint" class="note" style="display:none"></div>
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
  var R = d.role, C = d.local;
  document.getElementById('cards').innerHTML =
    card('this machine', d.self.id) +
    card('role', R.is_admin ? 'admin (merges for everyone)' : 'spoke') +
    card('admin', R.is_admin
                    ? (R.stale_url ? 'this machine (stale url: ' + R.stale_url + ')'
                                   : 'this machine')
                    : (R.admin_id || R.admin_url || '—')) +
    card('class A', C.class_a) + card('class B', C.class_b) +
    card('tombstones', C.tombstones) + card('conflicts', C.conflicts) +
    // one card per directory: each has a different writer, so this says where
    // knowledge actually is — mine, received, the fold, my local view.
    card('okf/ (mine)', C.okf) + card('peers/ (inbox)', C.peers) +
    card('mesh/', C.mesh) + card('view/ (local)', C.view);
  var rows;
  if (R.is_admin){
    document.getElementById('th1').textContent = 'Spoke';
    rows = (d.spokes || []).map(function(s){
      return '<tr><td class="mono">' + esc(s.id) + '</td>'
        + '<td><span class="pill spoke">spoke</span></td>'
        + '<td class="num">—</td><td class="num">—</td>'
        + '<td class="num">' + ago(s.last_seen) + '</td></tr>';
    }).join('') || '<tr><td colspan="5" class="dim">No spoke has pushed yet.</td></tr>';
  } else {
    document.getElementById('th1').textContent = 'Admin';
    var A = d.admin || {};
    var state = A.probed
      ? (A.reachable ? '<span class="dot"></span>up'
                     : '<span class="dot down"></span><span class="bad">down</span>'
                       + (A.error ? ' <span class="dim mono">' + esc(A.error) + '</span>' : ''))
      : '<span class="dim">not probed</span>';
    rows = '<tr><td class="mono">' + esc(R.admin_url) + '</td>'
      + '<td>' + state + '</td>'
      + '<td class="num">' + (A.latency_ms == null ? '—' : A.latency_ms + ' ms') + '</td>'
      + '<td class="num">' + (A.entries == null ? '—' : A.entries) + '</td>'
      + '<td class="num">—</td></tr>';
  }
  document.getElementById('rows').innerHTML = rows;
  var hint = document.getElementById('hint');
  if (R.is_admin){
    hint.style.display = 'block';
    hint.innerHTML = 'This machine is the <b>admin</b>: it receives every spoke\\'s '
      + 'notes, runs the compaction for all of them, and serves the result back. '
      + 'Point a spoke here by setting <span class="mono">AIFORGE_ADMIN_URL</span> '
      + 'on it. Sync answers with no credential unless '
      + '<span class="mono">AIFORGE_SYNC_AUTH=1</span> is set.';
  } else if (R.stranded){
    hint.style.display = 'block';
    hint.innerHTML = '<b>This machine neither syncs nor merges.</b> It is a spoke '
      + '(<span class="mono">AIFORGE_ROLE=spoke</span>) with no '
      + '<span class="mono">AIFORGE_ADMIN_URL</span> to sync with, so the '
      + 'cross-machine merge is skipped and nothing arrives. Set the url, or '
      + 'start this box with <span class="mono">./run.sh --admin</span>.';
  } else { hint.style.display = 'none'; }
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
