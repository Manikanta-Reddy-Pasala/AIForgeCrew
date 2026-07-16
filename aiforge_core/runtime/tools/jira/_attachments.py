"""Issue-attachment download + analysis — pull images/documents off an issue,
caption/extract them and optionally persist them into the ticket's work folder.

Split out of the former ``jira.py`` module; behaviour is unchanged.
"""
from __future__ import annotations

import os

from aiforge_core.runtime.tools import _http_integration as _http

from ._core import _base, _headers, _ssl_ctx, _TIMEOUT_S


def _max_images() -> int:
    try:
        return max(0, int(os.environ.get("AIFORGE_INTEGRATION_MAX_IMAGES", "4")))
    except ValueError:
        return 4


def _fetch_attachments(attachments: list, role: str = "doer",
                       save_ctx: tuple | None = None) -> list[dict]:
    """Download issue attachments — images AND documents (pdf/xlsx/docx/text) —
    and analyse them (vision caption for images, extracted text for docs) so the
    agent can use them as part of the task. When ``save_ctx`` = (kind, key) is
    given, each file is also SAVED into that context's folder
    (~/.aiforge/work/<kind>/<key>/attachments/) so the ticket's images persist
    across sessions. Best-effort, capped, never raises."""
    cap = _max_images()
    if cap <= 0 or not attachments:
        return []
    from aiforge_core.runtime import chat_media
    save_dir = None
    if save_ctx:
        try:
            from aiforge_core.runtime import work_context as _wc
            save_dir = _wc.attachments_dir(save_ctx[0], save_ctx[1])
        except Exception:  # noqa: BLE001
            save_dir = None
    out: list[dict] = []
    for a in attachments:
        if len(out) >= cap:
            break
        if not isinstance(a, dict):
            continue
        mime = (a.get("mimeType") or "").lower()
        url = a.get("content")
        name = a.get("filename") or "attachment"
        if not url or not chat_media.supported_attachment(mime, name):
            continue
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
    """Persist raw attachment bytes under ``save_dir`` (the ticket's folder).
    Returns the absolute path, or "" on failure — never raises."""
    import os as _os
    import re as _re
    safe = _re.sub(r"[^A-Za-z0-9._-]+", "_", name or "attachment").strip("_.") \
        or "attachment"
    try:
        base = _os.path.join(save_dir, safe)
        root, ext = _os.path.splitext(base)
        path, n = base, 1
        while _os.path.exists(path):
            # Re-read of the same ticket: if this exact file is already saved,
            # REUSE it (don't grow the folder with -1/-2 copies each visit).
            try:
                with open(path, "rb") as ex:
                    if ex.read() == raw:
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
