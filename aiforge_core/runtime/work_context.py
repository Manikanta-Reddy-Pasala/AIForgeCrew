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

_KINDS = ("jira", "confluence", "repo", "web")

# JIRA issue key: PROJ-123 / ABC1-45. Uppercase project + dash + digits.
_JIRA_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,20}-\d+)\b")
# Unambiguous: a Jira browse URL.
_JIRA_URL_RE = re.compile(r"/browse/([A-Z][A-Z0-9]{1,20}-\d+)\b")
# A bare KEY-123 token is only treated as a ticket when a Jira context word is
# also present — otherwise common ALL-CAPS-NUMBER tokens (UTF-8, GPT-4, SHA-256,
# ISO-8601, RFC-2119, CVE-2021, COVID-19 …) would false-positive and silently
# re-home a plain chat into a bogus work/jira/<token> folder.
# Deliberately UNAMBIGUOUS words only. Dropped bug/issue/story — they're common
# English, so "the UTF-8 issue" would false-bind on the token UTF-8.
_JIRA_SIGNAL_RE = re.compile(
    r"\b(jira|ticket|epic|sprint|backlog|sub-?task)\b",
    re.IGNORECASE)
# Confluence page id inside a URL (…/pages/12345/… or pageId=12345).
_CONF_URL_RE = re.compile(r"(?:/pages/|pageId=)(\d{4,})")


def _root() -> str:
    cfg = os.environ.get("AIFORGE_CONFIG_DIR", os.path.expanduser("~/.aiforge"))
    return os.path.join(os.path.expanduser(cfg), "work")


def _slug(key: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", (key or "").strip()).strip("-.")
    return s or "unknown"


# Generated dossier/attachment artifacts — git-ignored inside the context folder
# so a Jira/Confluence READ never surfaces them as "N files changed" in chat.
_DOSSIER_IGNORES = (
    "# AIForge dossier artifacts (generated on read — not code changes)",
    ".dossier.json", "dossier.md", "ticket.md", "page.md",
    "confluence-*.md", "jira-*.md", "attachments/",
)


def _ensure_gitignore(path: str) -> None:
    """Idempotently ensure the dossier patterns are git-ignored in this folder,
    so the chat 'changes' view (which intent-adds untracked, non-ignored files)
    never reports a plain read as edits. Never clobbers existing content."""
    gi = os.path.join(path, ".gitignore")
    existing = ""
    if os.path.exists(gi):
        try:
            with open(gi, encoding="utf-8") as f:
                existing = f.read()
        except OSError:
            return
    have = {ln.strip() for ln in existing.splitlines() if ln.strip()}
    missing = [ln for ln in _DOSSIER_IGNORES if ln not in have]
    if not missing:
        return
    chunk = ("" if (not existing or existing.endswith("\n")) else "\n") \
        + "\n".join(missing) + "\n"
    try:
        with open(gi, "a", encoding="utf-8") as f:
            f.write(chunk)
    except OSError:
        pass


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
            _ensure_gitignore(path)
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
    # An explicit Jira browse URL is unambiguous.
    m = _JIRA_URL_RE.search(text)
    if m:
        return ("jira", m.group(1))
    # A bare KEY-123 counts as a ticket ONLY alongside a Jira context word, so
    # ordinary tokens (UTF-8, GPT-4, …) never re-home a plain chat.
    m = _JIRA_RE.search(text)
    if m and _JIRA_SIGNAL_RE.search(text):
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
