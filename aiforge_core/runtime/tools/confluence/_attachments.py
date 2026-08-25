"""Page attachment listing/download + analysis + local persistence."""
from __future__ import annotations

import os
import sys

from .. import _http_integration as _http
from ._config import (_TIMEOUT_S, _base, _headers, _request, _ssl_ctx)


def _max_images() -> int:
    try:
        return max(0, int(os.environ.get("AIFORGE_INTEGRATION_MAX_IMAGES", "4")))
    except ValueError:
        return 4


def _attachment_rows(pid: str) -> list:
    """The page's attachment records, or [] when the listing fails.

    ``_request`` is resolved off the PACKAGE so tests that patch it on the
    top-level ``confluence`` module (the pre-split single-module namespace)
    still take effect here.
    """
    request = sys.modules[__package__]._request
    r = request("GET", f"/rest/api/content/{pid}/child/attachment",
                params={"limit": 50})
    if not r.get("ok"):
        return []
    data = r["data"] if isinstance(r["data"], dict) else {}
    return data.get("results") or []


def _page_attachment_dir(pid: str) -> str | None:
    """Persist this page's attachments into its OWN folder
    (work/confluence/<id>/) so they're there again next session —
    page-specific, not global."""
    try:
        from aiforge_core.runtime import work_context as _wc
        return _wc.attachments_dir("confluence", str(pid))
    except Exception:  # noqa: BLE001
        return None


def _wanted_attachment(a, chat_media) -> tuple[str, str, str] | None:
    """``(name, mime, download url)`` for an attachment worth fetching."""
    if not isinstance(a, dict):
        return None
    mime = ((a.get("extensions") or {}).get("mediaType") or "").lower()
    dl = (a.get("_links") or {}).get("download") or ""
    name = a.get("title") or "attachment"
    if not dl or not chat_media.supported_attachment(mime, name):
        return None
    return name, mime, (_base() + dl if dl.startswith("/") else dl)


def _download_and_analyse(name: str, mime: str, url: str, role: str,
                          save_dir: str | None, chat_media) -> dict:
    import urllib.parse as _up
    try:
        got = _http.http_get_bytes(url, headers=_headers(), timeout=_TIMEOUT_S,
                                   context=_ssl_ctx(),
                                   allow_host=_up.urlsplit(_base()).hostname)
        if not got.get("ok"):
            return {"filename": name, "description": "",
                    "error": got.get("error")}
        info = chat_media.analyze_attachment(name, got["bytes"], role,
                                             mime=mime)
        if save_dir:
            info["path"] = _save_attachment(save_dir, name, got["bytes"])
        return info
    except Exception as exc:  # noqa: BLE001
        return {"filename": name, "description": "", "error": str(exc)}


def _fetch_attachments(pid: str, role: str = "doer") -> list[dict]:
    """List a page's attachments, download images AND documents (pdf/xlsx/docx/
    text), and analyse them (vision caption / extracted text) so the agent uses
    them as part of the task. Best-effort, capped."""
    cap = _max_images()
    if cap <= 0 or not pid:
        return []
    results = _attachment_rows(pid)
    if not results:
        return []
    from aiforge_core.runtime import chat_media
    save_dir = _page_attachment_dir(pid)
    out: list[dict] = []
    for a in results:
        if len(out) >= cap:
            break
        wanted = _wanted_attachment(a, chat_media)
        if wanted is None:
            continue
        name, mime, url = wanted
        out.append(_download_and_analyse(name, mime, url, role, save_dir,
                                         chat_media))
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
