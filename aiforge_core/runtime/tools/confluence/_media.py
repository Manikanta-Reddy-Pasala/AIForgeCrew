"""media → storage-format macros + attachment upload/resolve helpers."""
from __future__ import annotations

import base64
import mimetypes
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .. import _http_integration as _http
from ._config import _TIMEOUT_S, _base, _conf, _configured, _ssl_ctx

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
_CODE_FENCE_RE = re.compile(r"```([A-Za-z0-9_+#.-]*+)[^\n]*\n(.*?)```", re.S)
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
    # An attachment is FILE CONTENT leaving the box, which is a bigger step
    # than posting a sentence — it gets its own switch, and it is a write, so
    # an unattended run has to be opted in.
    from aiforge_core.net import egress as _egress
    refusal = _egress.allow("integration", _conf().get("base_url") or "",
                            method="POST", upload=True)
    if refusal is not None:
        return refusal
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
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _resolve_image_bytes(src: str, cwd: str | None) -> tuple[bytes, str] | None:
    """Fetch an image ref → (bytes, content_type). http(s) is downloaded; a
    local path is resolved against cwd. None on any failure (skip that image)."""
    ct = mimetypes.guess_type(src)[0] or "application/octet-stream"
    if re.match(r"^https?://", src, re.I):
        # The <img src> is scraped from the page body the MODEL wrote, so this
        # is a model-composed URL like any other — it was fetched with no gate
        # and no SSRF guard, while the sibling attachment paths pin the host.
        from aiforge_core.net import egress as _egress
        if _egress.check(src) is not None:
            return None
        from aiforge_core.net.ssl import SSRFBlocked, guard_public_url
        try:
            guard_public_url(src)
        except SSRFBlocked as exc:
            if exc.kind != "dns":
                return None
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
