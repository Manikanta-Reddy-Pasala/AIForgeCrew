"""Confluence (Server / Data Center) tool — search / read / create / update pages.

Lets the chat agent pull a page in, analyse it, draft a new page, or edit an
existing one. Server/DC REST API v1 (``/rest/api/content``).

Config (env):
  CONFLUENCE_BASE_URL   e.g. https://confluence.internal  (no trailing /wiki)
  CONFLUENCE_TOKEN      Personal Access Token (Bearer) — or the password/token
                        for basic auth when CONFLUENCE_USER is also set
  CONFLUENCE_USER       (optional) username/email → switches to Basic auth
  CONFLUENCE_CA_BUNDLE=/path/ca.pem   trust an internal CA — verification STAYS ON
  CONFLUENCE_INSECURE_TLS=1   skip TLS verify entirely (last resort: the auth
                         token then travels over an unauthenticated channel)

Soft-error contract: every function returns ``{"ok": bool, ...}`` and never
raises into the agent loop. Page bodies are Confluence "storage" XHTML.

This module was split (grouped by concern) into ``_config`` / ``_media`` /
``_attachments`` / ``_tools`` submodules; this package re-exports the full former
top-level surface so ``from aiforge_core.runtime.tools import confluence`` and
every ``confluence.<name>`` attribute access is unchanged.
"""
from __future__ import annotations

from .. import _http_integration as _http
from ..confluence_format import md_to_storage
from ._config import (
    _BODY_CAP,
    _TIMEOUT_S,
    _auth_scheme,
    _base,
    _conf,
    _configured,
    _headers,
    _page_url,
    _request,
    _ssl_ctx,
    _truthy,
    default_space,
)
from ._media import (
    _CODE_FENCE_RE,
    _HTML_IMG_RE,
    _MD_IMG_RE,
    _MERMAID_FENCE_RE,
    _cdata,
    _code_macro,
    _diagram_mode,
    _mermaid_macro,
    _mermaid_macro_name,
    _resolve_image_bytes,
    _safe_filename,
    _storagify_media,
    _upload_attachment,
    _upload_page_images,
)
from ._attachments import (
    _fetch_attachments,
    _max_images,
    _save_attachment,
)
from ._tools import (
    confluence_add_label,
    confluence_attach,
    confluence_children,
    confluence_comment,
    confluence_comments,
    confluence_create,
    confluence_descendants,
    confluence_labels,
    confluence_page_by_title,
    confluence_read,
    confluence_resolve_space,
    confluence_search,
    confluence_spaces,
    confluence_test,
    confluence_update,
)

__all__ = ["confluence_search", "confluence_read", "confluence_create",
           "confluence_update", "confluence_children", "confluence_attach",
           "confluence_test"]
