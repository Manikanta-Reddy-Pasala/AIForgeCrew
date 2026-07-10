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
import mimetypes
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid

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


# ───────────────────── media → storage-format macros ────────────────────
#
# A page body handed to us is Confluence "storage" XHTML — but agents (and
# pasted markdown) routinely carry ```mermaid fences, ```code fences, and
# markdown/HTML <img> that Confluence does NOT render. We rewrite those into the
# proper storage macros: mermaid → the diagram macro (app name is env-tunable);
# code → the code macro; images → <ac:image><ri:attachment> AND the referenced
# files are uploaded as page attachments (create/update do this once the page
# id exists). Plain storage bodies (no fence / no image) pass through untouched.

def _mermaid_macro_name() -> str:
    """Confluence mermaid macro name — app-specific. Default 'mermaid' ('Mermaid
    for Confluence'); set AIFORGE_CONFLUENCE_MERMAID_MACRO for e.g.
    'mermaid-cloud' (Stratus)."""
    return (os.environ.get("AIFORGE_CONFLUENCE_MERMAID_MACRO") or "mermaid").strip()


def _cdata(text: str) -> str:
    """Wrap in CDATA, splitting any literal ``]]>`` so it can't close early."""
    return "<![CDATA[" + str(text).replace("]]>", "]]]]><![CDATA[>") + "]]>"


def _diagram_mode() -> str:
    """How to render ```mermaid blocks (env AIFORGE_CONFLUENCE_DIAGRAM):

    * 'code'    — a Confluence CODE macro holding the mermaid source, placed
                  where the diagram belongs. Renders on ANY instance (no app,
                  no conversion). DEFAULT.
    * 'mermaid' — a mermaid macro (needs a mermaid app installed).
    """
    v = (os.environ.get("AIFORGE_CONFLUENCE_DIAGRAM") or "code").strip().lower()
    return v if v in ("code", "mermaid") else "code"


def _mermaid_macro(code: str) -> str:
    return (f'<ac:structured-macro ac:name="{_mermaid_macro_name()}">'
            f'<ac:plain-text-body>{_cdata(code)}'
            f'</ac:plain-text-body></ac:structured-macro>')


def _code_macro(code: str, lang: str = "") -> str:
    param = (f'<ac:parameter ac:name="language">{lang}</ac:parameter>'
             if lang else "")
    return (f'<ac:structured-macro ac:name="code">{param}'
            f'<ac:plain-text-body>{_cdata(code)}'
            f'</ac:plain-text-body></ac:structured-macro>')


_MERMAID_FENCE_RE = re.compile(r"```mermaid[^\n]*\n(.*?)```", re.S | re.I)
_CODE_FENCE_RE = re.compile(r"```([A-Za-z0-9_+#.-]*)[^\n]*\n(.*?)```", re.S)
_MD_IMG_RE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_HTML_IMG_RE = re.compile(r"<img\b[^>]*\bsrc=[\"']([^\"']+)[\"'][^>]*/?>", re.I)


def _safe_filename(src: str) -> str:
    fn = os.path.basename(urllib.parse.urlparse(src).path) or "image"
    fn = re.sub(r"[^A-Za-z0-9._-]+", "_", fn).strip("_.")
    return fn or "image.png"


def _storagify_media(body: str) -> tuple[str, list[dict]]:
    """Rewrite mermaid/code fences + markdown/HTML images into storage macros.

    Returns ``(new_body, image_refs)`` where each ref is ``{filename, src}`` to
    be uploaded as a page attachment. No-op (body unchanged, no refs) when the
    body carries none of these constructs."""
    if "```" not in body and "![" not in body and "<img" not in body.lower():
        return body, []
    refs: list[dict] = []
    mode = _diagram_mode()

    def _mermaid(m):
        code = m.group(1).rstrip()
        if mode == "mermaid":
            return _mermaid_macro(code)
        # 'code' (default): a code macro with the mermaid source, in place —
        # renders on any instance, no diagram app required.
        return _code_macro(code, "mermaid")

    body = _MERMAID_FENCE_RE.sub(_mermaid, body)
    body = _CODE_FENCE_RE.sub(
        lambda m: _code_macro(m.group(2).rstrip(), m.group(1)), body)

    def _img(src: str) -> str:
        src = src.strip()
        fn = _safe_filename(src)
        if not any(r.get("src") == src and r["filename"] == fn for r in refs):
            refs.append({"filename": fn, "src": src})
        return f'<ac:image><ri:attachment ri:filename="{fn}"/></ac:image>'

    body = _MD_IMG_RE.sub(lambda m: _img(m.group(1)), body)
    body = _HTML_IMG_RE.sub(lambda m: _img(m.group(1)), body)
    return body, refs


def _upload_attachment(pid: str, filename: str, data: bytes,
                       content_type: str = "application/octet-stream") -> dict:
    """Upload (or replace) one attachment on a page via multipart. Idempotent:
    an existing same-name attachment reports ok. Never raises."""
    if not _configured():
        return {"ok": False, "error": "confluence_not_configured"}
    boundary = "----aiforge" + uuid.uuid4().hex
    pre = (f"--{boundary}\r\n"
           f'Content-Disposition: form-data; name="file"; filename="{filename}"'
           f"\r\nContent-Type: {content_type}\r\n\r\n").encode()
    payload = pre + data + f"\r\n--{boundary}--\r\n".encode()
    c = _conf()
    headers = {"X-Atlassian-Token": "nocheck",
               "User-Agent": "AIForgeCrew-Confluence/1.0",
               "Content-Type": f"multipart/form-data; boundary={boundary}"}
    if c["user"]:
        headers["Authorization"] = "Basic " + base64.b64encode(
            f"{c['user']}:{c['token']}".encode()).decode()
    else:
        headers["Authorization"] = "Bearer " + c["token"]
    url = _base() + f"/rest/api/content/{pid}/child/attachment"
    req = urllib.request.Request(url, data=payload, headers=headers,
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S,
                                    context=_ssl_ctx()) as resp:
            resp.read()
        return {"ok": True, "filename": filename}
    except urllib.error.HTTPError as e:  # noqa: PERF203
        msg = (e.read() or b"")[:300].decode(errors="replace")
        # Same-name attachment already present → treat as success (the page's
        # <ri:attachment> reference resolves either way).
        if e.code == 400 and ("already exist" in msg.lower()
                              or "same file name" in msg.lower()):
            return {"ok": True, "filename": filename, "note": "exists"}
        return {"ok": False, "error": f"http {e.code}: {msg}"}
    except Exception as exc:  # noqa: BLE001 — network/TLS, never fatal
        return {"ok": False, "error": str(exc)}


def _resolve_image_bytes(src: str, cwd: str | None) -> tuple[bytes, str] | None:
    """Fetch an image ref → (bytes, content_type). http(s) is downloaded; a
    local path is resolved against cwd. None on any failure (skip that image)."""
    ct = mimetypes.guess_type(src)[0] or "application/octet-stream"
    if re.match(r"^https?://", src, re.I):
        try:
            got = _http.http_get_bytes(src, headers={
                "User-Agent": "AIForgeCrew-Confluence/1.0"},
                timeout=_TIMEOUT_S, context=_ssl_ctx())
            data = got.get("bytes") if isinstance(got, dict) else got
            return (data, ct) if data else None
        except Exception:  # noqa: BLE001
            return None
    path = src if os.path.isabs(src) else os.path.join(cwd or ".", src)
    try:
        with open(path, "rb") as fh:
            return fh.read(), ct
    except OSError:
        return None


def _upload_page_images(pid: str, refs: list[dict], cwd: str | None) -> list[dict]:
    """Upload every image the body referenced (local read / http download).
    Returns per-image results."""
    results = []
    for ref in refs:
        got = _resolve_image_bytes(ref["src"], cwd)
        if got is None:
            results.append({"filename": ref["filename"], "ok": False,
                            "error": f"unresolved: {ref['src']}"})
            continue
        data, ct = got
        results.append(_upload_attachment(pid, ref["filename"], data, ct))
    return results


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
    # Rewrite mermaid/code fences + images into storage macros; images are
    # uploaded as attachments after the page exists (id needed).
    xhtml, img_refs = _storagify_media(str(args["body"]))
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
    xhtml, img_refs = _storagify_media(str(args["body"]))
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


__all__ = ["confluence_search", "confluence_read", "confluence_create",
           "confluence_update", "confluence_children", "confluence_attach",
           "confluence_test"]
