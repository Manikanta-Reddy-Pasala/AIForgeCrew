"""Context-keyed persistent workspaces.

A chat's working directory is normally an ephemeral ``session-<id>`` folder that
dies with the session. But work is usually ABOUT something durable — a Jira
ticket, a Confluence page, a repo — and that context outlives any one chat. So
instead of scattering a ticket's artifacts across throwaway session folders, we
keep ONE folder per context, SHARED across every session that touches it:

    ~/.aiforge/work/jira/<JIRA-ID>/          (a ticket — holds its images, the
                                              Confluence pages referenced in it,
                                              scratch work, notes)
    ~/.aiforge/work/confluence/<page-id>/    (a page, when that's the subject)
    ~/.aiforge/work/repo/<repo>/             (a repo's shared scratch + info)

Everything specific to a ticket lives INSIDE that ticket's folder (images are
ticket-specific, not global) and is there again next session. A plain chat with
no detectable context stays ephemeral (``session-<id>``).
"""
from __future__ import annotations

import os
import re

_KINDS = ("jira", "confluence", "repo")

# JIRA issue key: PROJ-123 / ABC1-45. Uppercase project + dash + digits.
_JIRA_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,20}-\d+)\b")
# Confluence page id inside a URL (…/pages/12345/… or pageId=12345).
_CONF_URL_RE = re.compile(r"(?:/pages/|pageId=)(\d{4,})")


def _root() -> str:
    cfg = os.environ.get("AIFORGE_CONFIG_DIR", os.path.expanduser("~/.aiforge"))
    return os.path.join(os.path.expanduser(cfg), "work")


def _slug(key: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", (key or "").strip()).strip("-.")
    return s or "unknown"


def context_dir(kind: str, key: str, *, create: bool = True) -> str:
    """Absolute path to the shared workspace for ``(kind, key)``. Created on
    demand. ``kind`` must be jira|confluence|repo; anything else falls back to a
    'misc' bucket so a bad key never escapes the work root."""
    kind = kind if kind in _KINDS else "misc"
    path = os.path.join(_root(), kind, _slug(key))
    if create:
        try:
            os.makedirs(path, exist_ok=True)
            os.makedirs(os.path.join(path, "attachments"), exist_ok=True)
        except OSError:
            pass
    return path


def attachments_dir(kind: str, key: str) -> str:
    """Where a context's downloaded images/docs live (inside its folder)."""
    d = os.path.join(context_dir(kind, key), "attachments")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


def detect_context(text: str) -> tuple[str, str] | None:
    """Best-effort (kind, key) from free text — a Jira key or a Confluence page
    URL/id. Returns None when nothing durable is referenced (→ plain chat).
    Jira wins over Confluence when both appear (the ticket is the container)."""
    if not text:
        return None
    m = _JIRA_RE.search(text)
    if m:
        return ("jira", m.group(1))
    m = _CONF_URL_RE.search(text)
    if m:
        return ("confluence", m.group(1))
    return None


def context_for_path(path: str | None) -> tuple[str, str] | None:
    """Reverse lookup: if ``path`` is a context workspace, return its (kind,key).
    Lets the attachment saver find the ticket folder from the active cwd."""
    if not path:
        return None
    try:
        root = os.path.realpath(_root())
        p = os.path.realpath(path)
    except OSError:
        return None
    if not p.startswith(root + os.sep):
        return None
    rel = os.path.relpath(p, root).split(os.sep)
    if len(rel) >= 2 and rel[0] in _KINDS:
        return (rel[0], rel[1])
    return None


__all__ = ["context_dir", "attachments_dir", "detect_context",
           "context_for_path"]
