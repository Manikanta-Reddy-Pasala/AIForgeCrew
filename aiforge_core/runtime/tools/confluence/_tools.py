"""Confluence agent tools — search / read / create / update / attach / ..."""
from __future__ import annotations

import sys
import urllib.parse

from ..confluence_format import inline_fragment, md_to_storage
from ..edit_merge import EditError, apply_edit
from ._config import (_BODY_CAP, _TIMEOUT_S, _auth_scheme, _base, _configured,
                      _headers, _page_url, _ssl_ctx, _truthy,
                      default_space)
from ._media import (_resolve_image_bytes, _safe_filename, _storagify_media,
                     _upload_attachment, _upload_page_images)
from ._attachments import _fetch_attachments

_REST_API_CONTENT = '/rest/api/content'
_MISSING_ID = "missing 'id'"


def _request(method, path, **kw):
    """Forward to the package-level ``_request`` at call time so tests that
    ``monkeypatch.setattr(confluence, "_request", ...)`` on the package are
    honoured by every tool here (this module was split out of the former
    single-file confluence.py; a plain import would bind the pre-patch object)."""
    return sys.modules[__package__]._request(method, path, **kw)


# ─────────────────────────── tools ──────────────────────────────────

def confluence_search(args: dict, _cwd: str | None = None) -> dict:
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


def _resolve_page_id(args: dict) -> "dict | str":
    """Resolve a page id from ``id`` or a ``title`` (+ optional ``space``)
    lookup. Returns the id string, or an error dict when it can't be resolved."""
    pid = args.get("id")
    if not pid and args.get("title"):
        params = {"title": args["title"], "expand": "version", "limit": 1}
        space = args.get("space") or default_space()
        if space:
            params["spaceKey"] = space
        rr = _request("GET", _REST_API_CONTENT, params=params)
        if not rr["ok"]:
            return rr
        res = (rr["data"].get("results") if isinstance(rr["data"], dict) else None) or []
        if not res:
            return {"ok": False, "error": "page_not_found"}
        pid = res[0].get("id")
    if not pid:
        return {"ok": False, "error": "missing 'id' or 'title'"}
    return pid


def _read_attachments(args: dict, doc_id) -> list:
    """Fetch + analyse a page's attachments (opt out with attachments=false).
    Resolves off the package so a test patching `_fetch_attachments` on the
    top-level `confluence` module (pre-split namespace) is honoured."""
    if not _truthy(str(args.get("attachments", args.get("images", "true")))):
        return []
    _fetch = sys.modules[__package__]._fetch_attachments
    return _fetch(str(doc_id)) or []


def confluence_read(args: dict, _cwd: str | None = None) -> dict:
    """Read a page (storage XHTML body). By ``id``, or ``title`` (+ optional
    ``space`` key)."""
    pid = _resolve_page_id(args)
    if isinstance(pid, dict):
        return pid                       # error dict from the lookup
    r = _request("GET", f"/rest/api/content/{pid}",
                 params={"expand": "body.storage,version,space"})
    if not r["ok"]:
        return r
    if not isinstance(r["data"], dict):
        return {"ok": False, "error": "page response too large or unreadable"}
    d = r["data"]
    body = (((d.get("body") or {}).get("storage") or {}).get("value") or "")
    out = {"ok": True, "id": d.get("id"), "title": d.get("title"),
           "space": (d.get("space") or {}).get("key"),
           "version": (d.get("version") or {}).get("number"),
           "body": body[:_BODY_CAP], "url": _page_url(d)}
    if len(body) > _BODY_CAP:
        # Never let a model rewrite a page it only saw part of.
        out["truncated"] = True
        out["body_chars"] = len(body)
    atts = _read_attachments(args, d.get("id") or pid)
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
    r = _request("POST", _REST_API_CONTENT, body=payload)
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


def _fragment_to_storage(body: str, mode: str) -> str:
    """The sent body as storage XHTML. A one-line ``replace_text`` swap stays
    INLINE — wrapping it in <p> would nest a paragraph inside the one it lands
    in — and keeps any markup or entities it copied from the page as-is."""
    if mode == "replace_text" and "\n" not in body:
        return inline_fragment(body)
    return md_to_storage(body)


def _sent(args: dict, mode: str) -> str:
    body = str(args.get("body") or "")
    return _fragment_to_storage(body, mode) if body else "(section/text removed)"


def merged_body(current: str, args: dict) -> "tuple[str, list]":
    """(new page body, image refs to upload) for an update ``args`` against
    the ``current`` storage body — the SAME merge the tool performs, so the
    approval preview shows exactly what will be written. Raises EditError."""
    mode = (args.get("mode") or "replace").strip().lower()
    body = str(args.get("body") or "")
    fragment, img_refs = (_storagify_media(_fragment_to_storage(body, mode))
                          if body else ("", []))
    return apply_edit(current, fragment, args, kind="storage"), img_refs


def confluence_update(args: dict, cwd: str | None = None) -> dict:
    """Edit a page. Required: ``id``, ``body``. ``mode`` says what ``body`` is:
    ``append`` / ``prepend`` (added to the page), ``replace_section`` (with
    ``section`` = heading text), ``replace_text`` (with ``find`` = exact text
    from the page), or ``replace`` (the WHOLE page; refused when it would drop
    most of the text or its tables/macros, unless ``allow_loss``). Optional
    ``title``. Merged into the live body, version auto-incremented."""
    pid = args.get("id")
    if not pid:
        return {"ok": False, "error": _MISSING_ID}
    mode = (args.get("mode") or "replace").strip().lower()
    # An EMPTY body deletes a section / a piece of text; anywhere else it is
    # a mistake (a whole page emptied, or nothing appended).
    if not args.get("body") and not (
            args.get("body") == "" and mode in ("replace_section", "replace_text")):
        return {"ok": False, "error": "missing 'body'"}
    cur = _request("GET", f"/rest/api/content/{pid}",
                   params={"expand": "version,body.storage"})
    if not cur["ok"]:
        return cur
    if not isinstance(cur["data"], dict):
        # Over the response cap (or not JSON): merging into "" would write the
        # fragment alone over the whole page.
        return {"ok": False, "error": "could not read the current page (response "
                "too large or unreadable) — not editing it"}
    d = cur["data"]
    next_ver = ((d.get("version") or {}).get("number") or 0) + 1
    title = args.get("title") or d.get("title")
    current = (((d.get("body") or {}).get("storage") or {}).get("value") or "")
    try:
        xhtml, img_refs = merged_body(current, args)
    except EditError as exc:
        return {"ok": False, "error": str(exc), "page_chars": len(current)}
    # Upload attachments FIRST (page id already exists) so the <ri:attachment>
    # references in the new body resolve as soon as the version is published.
    attachments = _upload_page_images(str(pid), img_refs, cwd) if img_refs else []
    payload = {
        "type": "page", "title": title,
        "version": {"number": next_ver},
        # Always storage: the merged body IS the page's storage XHTML.
        "body": {"storage": {"value": xhtml, "representation": "storage"}},
    }
    r = _request("PUT", f"/rest/api/content/{pid}", body=payload)
    if not r["ok"]:
        return r
    rd = r["data"] if isinstance(r["data"], dict) else {}
    out = {"ok": True, "id": pid, "version": next_ver, "title": title,
           "mode": (args.get("mode") or "replace"),
           "url": _page_url(rd),
           # What THIS edit wrote: the whole page for a replace, else the part
           # sent — the top of a long page says nothing about an append.
           "written": {"title": title,
                       "body": (xhtml if mode == "replace" else _sent(args, mode))[:2000]}}
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
        return {"ok": False, "error": _MISSING_ID}
    src = str(args.get("path") or args.get("url") or "").strip()
    if not src:
        return {"ok": False, "error": "missing 'path' or 'url'"}
    got = _resolve_image_bytes(src, cwd)
    if got is None:
        return {"ok": False, "error": f"could not read {src}"}
    data, ct = got
    filename = str(args.get("filename") or _safe_filename(src))
    return _upload_attachment(str(pid), filename, data, ct)


def confluence_children(args: dict, _cwd: str | None = None) -> dict:
    """List the child pages of a Confluence page. Required: ``id``."""
    pid = str(args.get("id") or "").strip()
    if not pid:
        return {"ok": False, "error": _MISSING_ID}
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


def confluence_spaces(args: dict, _cwd: str | None = None) -> dict:
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


def confluence_page_by_title(args: dict, _cwd: str | None = None) -> dict:
    """Find a page by exact ``title`` within a ``space`` (key). Returns id +
    version — the handle you need to update or comment on it."""
    space = (args.get("space") or default_space() or "").strip()
    title = (args.get("title") or "").strip()
    if not space or not title:
        return {"ok": False, "error": "space and title are required"}
    r = _request("GET", _REST_API_CONTENT,
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


def confluence_labels(args: dict, _cwd: str | None = None) -> dict:
    """Read the labels on a page. Required: ``id``."""
    pid = str(args.get("id") or "").strip()
    if not pid:
        return {"ok": False, "error": _MISSING_ID}
    r = _request("GET",
                 f"/rest/api/content/{urllib.parse.quote(pid)}/label")
    if not r["ok"]:
        return r
    d = r["data"] if isinstance(r["data"], dict) else {}
    labels = [x.get("name") for x in (d.get("results") or [])
              if isinstance(x, dict) and x.get("name")]
    return {"ok": True, "id": pid, "labels": labels}


def confluence_add_label(args: dict, _cwd: str | None = None) -> dict:
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


def confluence_comments(args: dict, _cwd: str | None = None) -> dict:
    """Read the comments on a page. Required: ``id``."""
    pid = str(args.get("id") or "").strip()
    if not pid:
        return {"ok": False, "error": _MISSING_ID}
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


def confluence_comment(args: dict, _cwd: str | None = None) -> dict:
    """Add a comment to a page. Required: ``id`` (page id), ``body`` (Markdown,
    or storage XHTML)."""
    pid = str(args.get("id") or "").strip()
    body = (args.get("body") or args.get("text") or "").strip()
    if not pid or not body:
        return {"ok": False, "error": "id and body are required"}
    # The agent writes Markdown; Confluence renders storage XHTML and showed
    # the comment's `**` / `#` / `- ` literally. Same conversion as a page
    # (storage input passes through unchanged).
    payload = {
        "type": "comment",
        "container": {"id": pid, "type": "page"},
        "body": {"storage": {"value": md_to_storage(body), "representation": "storage"}},
    }
    r = _request("POST", _REST_API_CONTENT, body=payload)
    if not r["ok"]:
        return r
    d = r["data"] if isinstance(r["data"], dict) else {}
    return {"ok": True, "id": d.get("id"), "page_id": pid}


def confluence_descendants(args: dict, _cwd: str | None = None) -> dict:
    """List ALL descendant pages of a page (deep, not just direct children).
    Required: ``id``."""
    pid = str(args.get("id") or "").strip()
    if not pid:
        return {"ok": False, "error": _MISSING_ID}
    r = _request("GET",
                 f"/rest/api/content/{urllib.parse.quote(pid)}/descendant/page",
                 params={"limit": int(args.get("limit", 100))})
    if not r["ok"]:
        return r
    d = r["data"] if isinstance(r["data"], dict) else {}
    kids = [{"id": c.get("id"), "title": c.get("title")}
            for c in (d.get("results") or []) if isinstance(c, dict)]
    return {"ok": True, "id": pid, "count": len(kids), "descendants": kids}


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
    if err.startswith(("http 401", "http 403")):
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
