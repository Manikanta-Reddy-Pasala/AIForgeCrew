"""Confluence (Server / Data Center) tool — search / read / create / update pages.

Lets the chat agent pull a page in, analyse it, draft a new page, or edit an
existing one. Server/DC REST API v1 (``/rest/api/content``).

Config (env):
  CONFLUENCE_BASE_URL   e.g. https://confluence.internal  (no trailing /wiki)
  CONFLUENCE_TOKEN      Personal Access Token (Bearer) — or the password/token
                        for basic auth when CONFLUENCE_USER is also set
  CONFLUENCE_USER       (optional) username/email → switches to Basic auth
  CONFLUENCE_INSECURE_TLS=1   skip TLS verify for a self-signed internal cert

Soft-error contract: every function returns ``{"ok": bool, ...}`` and never
raises into the agent loop. Page bodies are Confluence "storage" XHTML.
"""
from __future__ import annotations

import base64
import os
import urllib.parse

from . import _http_integration as _http

_TIMEOUT_S = 20
_BODY_CAP = 200_000

_truthy = _http.truthy


def _conf() -> dict:
    """Resolve config via the shared integration helper: base_url/token/
    insecure_tls (insecure by default) + user + default_space."""
    return _http.integration_conf(
        "confluence", "CONFLUENCE",
        str_fields=(("user", "CONFLUENCE_USER"),
                    ("default_space", "CONFLUENCE_DEFAULT_SPACE")))


def default_space() -> str:
    return _conf().get("default_space") or ""


def _auth_scheme() -> str:
    return "basic" if _conf()["user"] else "bearer"


def _base() -> str:
    return _conf()["base_url"]


def _configured() -> bool:
    c = _conf()
    return bool(c["base_url"] and c["token"])


def _headers() -> dict[str, str]:
    c = _conf()
    h = {"Content-Type": "application/json", "Accept": "application/json",
         "User-Agent": "AIForgeCrew-Confluence/1.0"}
    if c["user"]:
        h["Authorization"] = "Basic " + base64.b64encode(
            f"{c['user']}:{c['token']}".encode()).decode()
    else:
        h["Authorization"] = "Bearer " + c["token"]
    return h


def _ssl_ctx():
    return _http.ssl_context(_conf()["insecure_tls"])


def _request(method: str, path: str, *, params: dict | None = None,
             body: dict | None = None) -> dict:
    if not _configured():
        return {"ok": False, "error": "confluence_not_configured",
                "hint": "set CONFLUENCE_BASE_URL + CONFLUENCE_TOKEN"}
    url = _base() + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return _http.http_request(method, url, headers=_headers(), body=body,
                              timeout=_TIMEOUT_S, body_cap=_BODY_CAP,
                              context=_ssl_ctx(),
                              capture_headers=_http.ATLASSIAN_DENIED_HEADERS)


def _page_url(d: dict) -> str:
    links = d.get("_links") if isinstance(d.get("_links"), dict) else {}
    webui = links.get("webui") or ""
    return (_base() + webui) if webui else ""


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
        atts = _fetch_attachments(str(d.get("id") or pid))
        if atts:
            out["attachments"] = atts
    return out


def _max_images() -> int:
    try:
        return max(0, int(os.environ.get("AIFORGE_INTEGRATION_MAX_IMAGES", "4")))
    except ValueError:
        return 4


def _fetch_attachments(pid: str, role: str = "doer") -> list[dict]:
    """List a page's attachments, download images AND documents (pdf/xlsx/docx/
    text), and analyse them (vision caption / extracted text) so the agent uses
    them as part of the task. Best-effort, capped."""
    cap = _max_images()
    if cap <= 0 or not pid:
        return []
    r = _request("GET", f"/rest/api/content/{pid}/child/attachment",
                 params={"limit": 50})
    if not r.get("ok"):
        return []
    results = (r["data"].get("results") if isinstance(r["data"], dict) else None) or []
    from aiforge_core.runtime import chat_media
    # Persist this page's attachments into its own folder (work/confluence/<id>/)
    # so they're there again next session — page-specific, not global.
    save_dir = None
    try:
        from aiforge_core.runtime import work_context as _wc
        save_dir = _wc.attachments_dir("confluence", str(pid))
    except Exception:  # noqa: BLE001
        save_dir = None
    out: list[dict] = []
    for a in results:
        if len(out) >= cap:
            break
        if not isinstance(a, dict):
            continue
        mime = ((a.get("extensions") or {}).get("mediaType") or "").lower()
        dl = ((a.get("_links") or {}).get("download") or "")
        name = a.get("title") or "attachment"
        if not dl or not chat_media.supported_attachment(mime, name):
            continue
        url = _base() + dl if dl.startswith("/") else dl
        try:
            import urllib.parse as _up
            got = _http.http_get_bytes(url, headers=_headers(),
                                       timeout=_TIMEOUT_S, context=_ssl_ctx(),
                                       allow_host=_up.urlsplit(_base()).hostname)
            if not got.get("ok"):
                out.append({"filename": name, "description": "",
                            "error": got.get("error")})
                continue
            info = chat_media.analyze_attachment(name, got["bytes"],
                                                 role, mime=mime)
            if save_dir:
                info["path"] = _save_attachment(save_dir, name, got["bytes"])
            out.append(info)
        except Exception as exc:  # noqa: BLE001
            out.append({"filename": name, "description": "", "error": str(exc)})
    return out


def _save_attachment(save_dir: str, name: str, raw: bytes) -> str:
    import os as _os
    import re as _re
    safe = _re.sub(r"[^A-Za-z0-9._-]+", "_", name or "attachment").strip("_.") \
        or "attachment"
    try:
        base = _os.path.join(save_dir, safe)
        root, ext = _os.path.splitext(base)
        path, n = base, 1
        while _os.path.exists(path):
            try:
                with open(path, "rb") as ex:
                    if ex.read() == raw:   # already saved this exact file → reuse
                        return path
            except OSError:
                pass
            path = f"{root}-{n}{ext}"   # distinct content, same name → uniquify
            n += 1
        with open(path, "wb") as fh:
            fh.write(raw)
        return path
    except OSError:
        return ""


def confluence_create(args: dict, cwd: str | None = None) -> dict:
    """Create a page. Required: ``title``, ``space`` (key), ``body`` (storage
    XHTML). Optional: ``parent_id``, ``representation`` (storage|wiki)."""
    if not args.get("space") and default_space():
        args = {**args, "space": default_space()}
    for k in ("title", "space", "body"):
        if not args.get(k):
            return {"ok": False, "error": f"missing '{k}'"}
    payload: dict = {
        "type": "page", "title": args["title"],
        "space": {"key": args["space"]},
        "body": {"storage": {"value": args["body"],
                             "representation": args.get("representation", "storage")}},
    }
    if args.get("parent_id"):
        payload["ancestors"] = [{"id": str(args["parent_id"])}]
    r = _request("POST", "/rest/api/content", body=payload)
    if not r["ok"]:
        return r
    d = r["data"] if isinstance(r["data"], dict) else {}
    return {"ok": True, "id": d.get("id"), "title": d.get("title"),
            "url": _page_url(d),
            "written": {"title": d.get("title") or args["title"],
                        "body": str(args["body"])[:2000]}}


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
    payload = {
        "type": "page", "title": title,
        "version": {"number": next_ver},
        "body": {"storage": {"value": args["body"],
                             "representation": args.get("representation", "storage")}},
    }
    r = _request("PUT", f"/rest/api/content/{pid}", body=payload)
    if not r["ok"]:
        return r
    rd = r["data"] if isinstance(r["data"], dict) else {}
    return {"ok": True, "id": pid, "version": next_ver, "title": title,
            "url": _page_url(rd),
            "written": {"title": title, "body": str(args["body"])[:2000]}}


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


__all__ = ["confluence_search", "confluence_read", "confluence_create",
           "confluence_update", "confluence_children", "confluence_attach",
           "confluence_test"]
