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


def _fetch_attachments(pid: str, role: str = "doer") -> list[dict]:
    """List a page's attachments, download images AND documents (pdf/xlsx/docx/
    text), and analyse them (vision caption / extracted text) so the agent uses
    them as part of the task. Best-effort, capped."""
    cap = _max_images()
    if cap <= 0 or not pid:
        return []
    # Resolve `_request` off the package so tests that patch it on the top-level
    # `confluence` module (the pre-split single-module namespace) still take
    # effect here.
    _request = sys.modules[__package__]._request
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
