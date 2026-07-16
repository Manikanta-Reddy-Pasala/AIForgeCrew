"""Confluence agent tools — search / read / create / update / attach / ..."""
from __future__ import annotations

import sys
import urllib.parse

from ..confluence_format import md_to_storage
from ._config import (_BODY_CAP, _TIMEOUT_S, _auth_scheme, _base, _configured,
                      _headers, _page_url, _ssl_ctx, _truthy,
                      default_space)
from ._media import (_resolve_image_bytes, _safe_filename, _storagify_media,
                     _upload_attachment, _upload_page_images)
from ._attachments import _fetch_attachments


def _request(method, path, **kw):
    """Forward to the package-level ``_request`` at call time so tests that
    ``monkeypatch.setattr(confluence, "_request", ...)`` on the package are
    honoured by every tool here (this module was split out of the former
    single-file confluence.py; a plain import would bind the pre-patch object)."""
    return sys.modules[__package__]._request(method, path, **kw)


# ─────────────────────────── tools ──────────────────────────────────

def confluence_search(args: dict, cwd: str | None = None) -> dict:
    """Find pages. ``cql`` (raw CQL) OR ``query`` (full-text). ``limit``."""
    cql = (args.get("cql") or "").strip()
    if not cql and args.get("query"):
        q = str(args["query"]).replace('"', '\\"')
        cql = f'text ~ "{q}"'
    if not cql:
        return {"ok": False, "error": "missing 'query' or 'cql'"}
    # Scope to the default space when the caller didn't name one — otherwise a
    # bare "text ~ ..." searches every space (a common cause of a wrong/empty
    # result set). Explicit space=/CQL space is left untouched.
    space = (args.get("space") or default_space() or "").strip()
    if space and "space" not in cql.lower():
        # Escape quotes so a space value can't break out of the CQL literal
        # (same treatment as the query text above).
        _sp = space.replace('"', '\\"')
        cql = f'space = "{_sp}" AND ({cql})'
    r = _request("GET", "/rest/api/content/search",
                 params={"cql": cql, "limit": int(args.get("limit", 10)),
                         "expand": "space,version"})
    if not r["ok"]:
        return r
    data = r["data"] if isinstance(r["data"], dict) else {}
    out = [{"id": x.get("id"), "title": x.get("title"), "type": x.get("type"),
            "space": (x.get("space") or {}).get("key")}
           for x in (data.get("results") or [])]
    return {"ok": True, "results": out}


def confluence_read(args: dict, cwd: str | None = None) -> dict:
    """Read a page (storage XHTML body). By ``id``, or ``title`` (+ optional
    ``space`` key)."""
    pid = args.get("id")
    if not pid and args.get("title"):
        params = {"title": args["title"], "expand": "version", "limit": 1}
        _space = args.get("space") or default_space()
        if _space:
            params["spaceKey"] = _space
        rr = _request("GET", "/rest/api/content", params=params)
        if not rr["ok"]:
            return rr
        res = (rr["data"].get("results") if isinstance(rr["data"], dict) else None) or []
        if not res:
            return {"ok": False, "error": "page_not_found"}
        pid = res[0].get("id")
    if not pid:
        return {"ok": False, "error": "missing 'id' or 'title'"}
    r = _request("GET", f"/rest/api/content/{pid}",
                 params={"expand": "body.storage,version,space"})
    if not r["ok"]:
        return r
    d = r["data"] if isinstance(r["data"], dict) else {}
    body = (((d.get("body") or {}).get("storage") or {}).get("value") or "")
    out = {"ok": True, "id": d.get("id"), "title": d.get("title"),
           "space": (d.get("space") or {}).get("key"),
           "version": (d.get("version") or {}).get("number"),
           "body": body[:_BODY_CAP], "url": _page_url(d)}
    # Pull attachments (images + documents) + analyse them so the agent uses
    # them as part of the task (opt out with attachments=false). Best-effort.
    if _truthy(str(args.get("attachments", args.get("images", "true")))):
        # Resolve off the package so a test patching `_fetch_attachments` on the
        # top-level `confluence` module (pre-split namespace) is honoured.
        _fetch = sys.modules[__package__]._fetch_attachments
        atts = _fetch(str(d.get("id") or pid))
        if atts:
            out["attachments"] = atts
    return out


def confluence_create(args: dict, cwd: str | None = None) -> dict:
    """Create a page. Required: ``title``, ``space`` (key), ``body`` (storage
    XHTML). Optional: ``parent_id``, ``representation`` (storage|wiki)."""
    if not args.get("space") and default_space():
        args = {**args, "space": default_space()}
    for k in ("title", "space", "body"):
        if not args.get(k):
            return {"ok": False, "error": f"missing '{k}'"}
    # Rewrite mermaid/code fences + images into storage macros; images are
    # uploaded as attachments after the page exists (id needed).
    xhtml, img_refs = _storagify_media(md_to_storage(str(args["body"])))
    payload: dict = {
        "type": "page", "title": args["title"],
        "space": {"key": args["space"]},
        "body": {"storage": {"value": xhtml,
                             "representation": args.get("representation", "storage")}},
    }
    if args.get("parent_id"):
        payload["ancestors"] = [{"id": str(args["parent_id"])}]
    r = _request("POST", "/rest/api/content", body=payload)
    if not r["ok"]:
        return r
    d = r["data"] if isinstance(r["data"], dict) else {}
    out = {"ok": True, "id": d.get("id"), "title": d.get("title"),
           "url": _page_url(d),
           "written": {"title": d.get("title") or args["title"],
                       "body": xhtml[:2000]}}
    if img_refs and d.get("id"):
        out["attachments"] = _upload_page_images(str(d["id"]), img_refs, cwd)
    return out


def confluence_update(args: dict, cwd: str | None = None) -> dict:
    """Update a page body. Required: ``id``, ``body``. Optional: ``title``,
    ``representation``. Version is auto-incremented (reads current first)."""
    pid = args.get("id")
    if not pid:
        return {"ok": False, "error": "missing 'id'"}
    if not args.get("body"):
        return {"ok": False, "error": "missing 'body'"}
    cur = _request("GET", f"/rest/api/content/{pid}", params={"expand": "version"})
    if not cur["ok"]:
        return cur
    d = cur["data"] if isinstance(cur["data"], dict) else {}
    next_ver = ((d.get("version") or {}).get("number") or 0) + 1
    title = args.get("title") or d.get("title")
    xhtml, img_refs = _storagify_media(md_to_storage(str(args["body"])))
    # Upload attachments FIRST (page id already exists) so the <ri:attachment>
    # references in the new body resolve as soon as the version is published.
    attachments = _upload_page_images(str(pid), img_refs, cwd) if img_refs else []
    payload = {
        "type": "page", "title": title,
        "version": {"number": next_ver},
        "body": {"storage": {"value": xhtml,
                             "representation": args.get("representation", "storage")}},
    }
    r = _request("PUT", f"/rest/api/content/{pid}", body=payload)
    if not r["ok"]:
        return r
    rd = r["data"] if isinstance(r["data"], dict) else {}
    out = {"ok": True, "id": pid, "version": next_ver, "title": title,
           "url": _page_url(rd), "written": {"title": title, "body": xhtml[:2000]}}
    if attachments:
        out["attachments"] = attachments
    return out


def confluence_attach(args: dict, cwd: str | None = None) -> dict:
    """Upload a file as a page attachment. Required: ``id`` (page id) and
    ``path`` (local file) OR ``url`` (http(s) to fetch). Optional ``filename``
    to override the stored name. Reference it in the page body with
    ``<ac:image><ri:attachment ri:filename="NAME"/></ac:image>`` (images) or the
    view-file macro (docs). create/update do this automatically for images in
    the body — use this for a standalone upload."""
    pid = args.get("id")
    if not pid:
        return {"ok": False, "error": "missing 'id'"}
    src = str(args.get("path") or args.get("url") or "").strip()
    if not src:
        return {"ok": False, "error": "missing 'path' or 'url'"}
    got = _resolve_image_bytes(src, cwd)
    if got is None:
        return {"ok": False, "error": f"could not read {src}"}
    data, ct = got
    filename = str(args.get("filename") or _safe_filename(src))
    return _upload_attachment(str(pid), filename, data, ct)


def confluence_children(args: dict, cwd: str | None = None) -> dict:
    """List the child pages of a Confluence page. Required: ``id``."""
    pid = str(args.get("id") or "").strip()
    if not pid:
        return {"ok": False, "error": "missing 'id'"}
    r = _request("GET",
                 f"/rest/api/content/{urllib.parse.quote(pid)}/child/page",
                 params={"limit": int(args.get("limit", 50))})
    if not r["ok"]:
        return r
    d = r["data"] if isinstance(r["data"], dict) else {}
    kids = [{"id": c.get("id"), "title": c.get("title")}
            for c in (d.get("results") or [])]
    return {"ok": True, "id": pid, "count": len(kids), "children": kids}


def confluence_resolve_space(args: dict, cwd: str | None = None) -> dict:
    """Resolve a LOOSELY-typed space name/key to the real Confluence space key —
    case, spaces, missing hyphens, small typos tolerated. Returns
    ``{ok, key, name, match}`` or candidates when ambiguous/none."""
    name = (args.get("name") or args.get("space") or args.get("query")
            or "").strip()
    if not name:
        return {"ok": False, "error": "missing 'name'"}
    r = confluence_spaces({"limit": 500}, cwd)
    if not r.get("ok"):
        return r
    cands: dict = {}
    for s in r.get("spaces") or []:
        k = s.get("key")
        if not k:
            continue
        cands[k] = k
        if s.get("name"):
            cands[s["name"]] = k
    from aiforge_core.config.repo_map import fuzzy_pick
    return fuzzy_pick(name, cands, value_key="key")


def confluence_spaces(args: dict, cwd: str | None = None) -> dict:
    """List the spaces the token can see (key, name, type)."""
    r = _request("GET", "/rest/api/space",
                 params={"limit": int(args.get("limit", 50)),
                         "type": args.get("type", "global")})
    if not r["ok"]:
        return r
    d = r["data"] if isinstance(r["data"], dict) else {}
    out = [{"key": s.get("key"), "name": s.get("name"), "type": s.get("type")}
           for s in (d.get("results") or []) if isinstance(s, dict)]
    return {"ok": True, "spaces": out, "count": len(out)}


def confluence_page_by_title(args: dict, cwd: str | None = None) -> dict:
    """Find a page by exact ``title`` within a ``space`` (key). Returns id +
    version — the handle you need to update or comment on it."""
    space = (args.get("space") or default_space() or "").strip()
    title = (args.get("title") or "").strip()
    if not space or not title:
        return {"ok": False, "error": "space and title are required"}
    r = _request("GET", "/rest/api/content",
                 params={"spaceKey": space, "title": title,
                         "expand": "version", "limit": 5})
    if not r["ok"]:
        return r
    d = r["data"] if isinstance(r["data"], dict) else {}
    res = d.get("results") or []
    if not res:
        return {"ok": True, "found": False, "space": space, "title": title}
    p = res[0]
    return {"ok": True, "found": True, "id": p.get("id"),
            "title": p.get("title"),
            "version": ((p.get("version") or {}) or {}).get("number"),
            "url": _page_url(p)}


def confluence_labels(args: dict, cwd: str | None = None) -> dict:
    """Read the labels on a page. Required: ``id``."""
    pid = str(args.get("id") or "").strip()
    if not pid:
        return {"ok": False, "error": "missing 'id'"}
    r = _request("GET",
                 f"/rest/api/content/{urllib.parse.quote(pid)}/label")
    if not r["ok"]:
        return r
    d = r["data"] if isinstance(r["data"], dict) else {}
    labels = [x.get("name") for x in (d.get("results") or [])
              if isinstance(x, dict) and x.get("name")]
    return {"ok": True, "id": pid, "labels": labels}


def confluence_add_label(args: dict, cwd: str | None = None) -> dict:
    """Add one or more labels to a page. Required: ``id``, ``labels`` (list or
    comma string)."""
    pid = str(args.get("id") or "").strip()
    labels = args.get("labels")
    if isinstance(labels, str):
        labels = [x.strip() for x in labels.split(",") if x.strip()]
    if not pid or not labels:
        return {"ok": False, "error": "id and labels are required"}
    body = [{"prefix": "global", "name": str(x)} for x in labels]
    r = _request("POST",
                 f"/rest/api/content/{urllib.parse.quote(pid)}/label", body=body)
    if not r["ok"]:
        return r
    return {"ok": True, "id": pid, "added": labels}


def confluence_comments(args: dict, cwd: str | None = None) -> dict:
    """Read the comments on a page. Required: ``id``."""
    pid = str(args.get("id") or "").strip()
    if not pid:
        return {"ok": False, "error": "missing 'id'"}
    r = _request("GET",
                 f"/rest/api/content/{urllib.parse.quote(pid)}/child/comment",
                 params={"expand": "body.storage", "limit":
                         int(args.get("limit", 25))})
    if not r["ok"]:
        return r
    d = r["data"] if isinstance(r["data"], dict) else {}
    out = []
    for c in (d.get("results") or []):
        if not isinstance(c, dict):
            continue
        val = (((c.get("body") or {}).get("storage") or {}).get("value") or "")
        out.append({"id": c.get("id"), "body": val[:2000]})
    return {"ok": True, "id": pid, "count": len(out), "comments": out}


def confluence_comment(args: dict, cwd: str | None = None) -> dict:
    """Add a comment to a page. Required: ``id`` (page id), ``body`` (storage
    XHTML or plain text)."""
    pid = str(args.get("id") or "").strip()
    body = (args.get("body") or args.get("text") or "").strip()
    if not pid or not body:
        return {"ok": False, "error": "id and body are required"}
    payload = {
        "type": "comment",
        "container": {"id": pid, "type": "page"},
        "body": {"storage": {"value": body,
                             "representation": args.get("representation",
                                                        "storage")}},
    }
    r = _request("POST", "/rest/api/content", body=payload)
    if not r["ok"]:
        return r
    d = r["data"] if isinstance(r["data"], dict) else {}
    return {"ok": True, "id": d.get("id"), "page_id": pid}


def confluence_descendants(args: dict, cwd: str | None = None) -> dict:
    """List ALL descendant pages of a page (deep, not just direct children).
    Required: ``id``."""
    pid = str(args.get("id") or "").strip()
    if not pid:
        return {"ok": False, "error": "missing 'id'"}
    r = _request("GET",
                 f"/rest/api/content/{urllib.parse.quote(pid)}/descendant/page",
                 params={"limit": int(args.get("limit", 100))})
    if not r["ok"]:
        return r
    d = r["data"] if isinstance(r["data"], dict) else {}
    kids = [{"id": c.get("id"), "title": c.get("title")}
            for c in (d.get("results") or []) if isinstance(c, dict)]
    return {"ok": True, "id": pid, "count": len(kids), "descendants": kids}


def confluence_attach(args: dict, cwd: str | None = None) -> dict:
    """Attach a LOCAL file to a Confluence page. Required: ``id`` (page id),
    ``path`` (local file path). Uses a multipart upload (the JSON _request
    helper can't, so this builds the request directly)."""
    import mimetypes
    import os as _os
    import urllib.request as _ur
    if not _configured():
        return {"ok": False, "error": "confluence_not_configured"}
    pid = str(args.get("id") or "").strip()
    path = str(args.get("path") or "").strip()
    if not pid or not path:
        return {"ok": False, "error": "need 'id' + local 'path'"}
    if not _os.path.isfile(path):
        return {"ok": False, "error": f"file not found: {path}"}
    try:
        with open(path, "rb") as fh:
            payload = fh.read()
    except OSError as exc:
        return {"ok": False, "error": f"read_failed: {exc}"}
    fname = _os.path.basename(path)
    ctype = mimetypes.guess_type(fname)[0] or "application/octet-stream"
    boundary = "----AIForgeBoundary7MA4YWxkTrZu0gW"
    body = b"".join([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{fname}"\r\n'
        .encode(),
        f"Content-Type: {ctype}\r\n\r\n".encode(),
        payload, b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ])
    headers = _headers()
    headers.pop("Content-Type", None)
    headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    headers["X-Atlassian-Token"] = "no-check"   # required for attachments
    url = _base() + f"/rest/api/content/{urllib.parse.quote(pid)}/child/attachment"
    req = _ur.Request(url, data=body, headers=headers, method="POST")
    try:
        with _ur.urlopen(req, timeout=_TIMEOUT_S, context=_ssl_ctx()) as resp:
            import json as _json
            raw = resp.read(_BODY_CAP)
            data = _json.loads(raw) if raw else {}
    except Exception as exc:  # noqa: BLE001 — soft-fail like the JSON helper
        return {"ok": False, "error": f"attach_failed: {str(exc)[:300]}"}
    results = data.get("results") if isinstance(data, dict) else None
    aid = (results[0].get("id") if results else None)
    return {"ok": True, "id": pid, "attachment_id": aid, "filename": fname}


def confluence_test() -> dict:
    """Connectivity + auth check for the Settings UI. Hits a cheap endpoint
    and, on auth failure, explains the most likely cause."""
    if not _configured():
        return {"ok": False, "error": "confluence_not_configured"}
    scheme = _auth_scheme()
    r = _request("GET", "/rest/api/space", params={"limit": 1})
    if r.get("ok"):
        return {"ok": True, "base_url": _base(), "auth": scheme}
    # Enrich auth errors with an actionable hint.
    err = str(r.get("error", ""))
    out = {**r, "auth": scheme, "base_url": _base()}
    if err.startswith("http 401") or err.startswith("http 403"):
        if scheme == "basic":
            out["hint"] = ("Using BASIC auth (User field is filled). A Personal "
                           "Access Token must be sent as Bearer — clear the User "
                           "field to use the token directly. Only fill User for "
                           "username+password basic auth.")
        else:
            out["hint"] = ("Bearer/PAT rejected. Check the token is a Confluence "
                           "Personal Access Token (not an API key/password), not "
                           "expired, and has read scope; and that Base URL has no "
                           "extra context path (e.g. trailing /wiki is Cloud only).")
    return out
