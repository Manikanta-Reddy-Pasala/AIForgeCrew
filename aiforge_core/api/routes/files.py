"""Ticket file-attachment serving (/files/{identifier}/{name}) — split out of api.py.

Serves operator-uploaded files persisted by ``_persist_ticket_attachments``
under ``{ticket-files-base}/{identifier}/``. A dynamic route (not a StaticFiles
mount) so it can resolve each ticket's real write location from
``metadata.attached_files[].abs_path`` before falling back to the persistent
base — the runner rebinds ``AIFORGE_REPO_ROOT`` per ticket, so a boot-time
static mount root would 404 every per-ticket upload. Behaviour VERBATIM.
"""
from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from aiforge_core.api._shared import _ticket_files_base
from aiforge_core.tickets import store as tickets_mod

router = APIRouter()


# Serve from the SAME persistent base uploads are written to
# (``_ticket_files_base``) — previously this used AIFORGE_REPO_ROOT, which in
# Docker pointed at an ephemeral HOME dir, so attachments 404'd after any
# container recreate.
_TICKET_FILES_ROOT = str(_ticket_files_base())
try:
    os.makedirs(_TICKET_FILES_ROOT, exist_ok=True)
except OSError:
    # Never let an unwritable attachments dir crash API boot; the mount uses
    # check_dir=False and uploads makedirs(parents=True) on demand.
    pass


def _attachment_candidates(identifier: str, safe_name: str) -> list:
    """Ordered on-disk candidate paths for a ticket attachment: the recorded
    ``abs_path`` (the real per-ticket-worktree write location), then the
    persistent base dir (current env), then the boot-time mount root."""
    from pathlib import Path as _Path
    candidates: list = []
    try:
        t = tickets_mod.get_enriched(identifier)
    except Exception:  # noqa: BLE001 — a store hiccup must not 500 the asset
        t = None
    if t:
        for f in ((t.get("metadata") or {}).get("attached_files") or []):
            if isinstance(f, dict) and (f.get("name") or "") == safe_name:
                ap = f.get("abs_path")
                if ap:
                    candidates.append(_Path(ap))
    candidates.append(_ticket_files_base() / identifier / safe_name)
    candidates.append(_Path(_TICKET_FILES_ROOT) / identifier / safe_name)
    return candidates


@router.get("/files/{identifier}/{name}")
def serve_ticket_file(identifier: str, name: str):
    """Serve a ticket attachment by (ticket, filename).

    A dynamic route rather than a ``StaticFiles`` mount: the mount binds ONE
    directory at import time, but the runner rebinds ``AIFORGE_REPO_ROOT`` per
    ticket, so uploads land in a per-ticket worktree that the boot-time mount
    root does not point at → every such attachment 404'd. The ticket's
    ``metadata.attached_files[].abs_path`` records the real write location, so
    resolve from there first, then fall back to the persistent base dir.
    """
    from pathlib import Path as _Path
    safe_name = _Path(name).name  # contain path traversal to the ticket dir
    for p in _attachment_candidates(identifier, safe_name):
        try:
            if p.is_file():
                return FileResponse(str(p))
        except OSError:
            continue
    raise HTTPException(404, f"attachment {identifier}/{safe_name} not found")
