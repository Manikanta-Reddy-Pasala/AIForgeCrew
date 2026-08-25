"""Cross-reference link + tag canonicalization for managed notes."""
from __future__ import annotations

import re

from ._helpers import (
    _BRIEF_REF_RE,
    _CONF_URL_RE,
    _JIRA_URL_RE,
    _MD_REF_RE,
    _PRIMARY_NOTE,
    _WIKI_REF_RE,
)


def _md_ref(ref_kind: str, ref_key: str) -> str:
    """The canonical cross-reference: a relative markdown link to the target
    dossier's primary md file. The path segment uses work_context's slug so
    the link resolves to the folder that ACTUALLY exists on disk."""
    from aiforge_core.runtime import work_context
    slug = work_context._slug(str(ref_key))
    return (f"[{ref_kind}/{ref_key}]"
            f"(../../{ref_kind}/{slug}/{_PRIMARY_NOTE.get(ref_kind, 'dossier.md')})")


def _canonical_link(s: str, kind: str, key: str) -> "str | None":
    """The canonical form of one link string, or None to drop it.

    A managed-dossier reference (an md ref, a legacy ``[[kind/key]]`` wiki ref,
    or a Jira/Confluence URL for ANOTHER dossier) becomes the relative markdown
    FILE LINK ``[kind/key](../../kind/key/ticket.md)``; a sibling-brief mapping
    link and the note's OWN source URL stay as-is; a non-http(s) scheme is
    dropped (a persisted note is shared state — file:///javascript: must never
    land in it).
    """
    mm = _MD_REF_RE.match(s)
    if mm:
        return _md_ref(mm.group(1), mm.group(2))   # re-canonicalize label/file drift
    wm = _WIKI_REF_RE.match(s)
    if wm:
        return _md_ref(wm.group(1), wm.group(2))
    if _BRIEF_REF_RE.match(s):
        return s                                    # sibling-brief mapping — canonical
    if not re.match(r"^https?://", s, re.IGNORECASE):
        return None                                 # scheme filter — http(s) only
    jm = _JIRA_URL_RE.search(s)
    if jm and not (kind == "jira" and jm.group(1) == str(key)):
        return _md_ref("jira", jm.group(1))
    cm = _CONF_URL_RE.search(s)
    if cm and not (kind == "confluence" and cm.group(1) == str(key)):
        return _md_ref("confluence", cm.group(1))
    return s                                         # the note's own source URL


def normalize_links(links, kind: str, key: str) -> list[str]:
    """Canonicalize a link list for a ``(kind, key)`` note.

    Only http(s) URLs pass the scheme filter; a reference to another managed
    dossier becomes the canonical relative markdown file link while the note's
    OWN canonical URL stays a URL; everything is deduped, order preserved (first
    occurrence wins) so output is deterministic.
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in links or []:
        if not isinstance(raw, str):
            continue
        s = raw.strip()
        if not s:
            continue
        canonical = _canonical_link(s, kind, key)
        if canonical and canonical not in seen:
            seen.add(canonical)
            out.append(canonical)
    return out


def normalize_tags(tags) -> list[str]:
    """Canonicalize a tags list: lowercased, whitespace→'-', deduped, order
    preserved. Accepts a list or a comma/space string. Non-strings dropped."""
    if isinstance(tags, str):
        tags = re.split(r"[,\s]+", tags)
    out: list[str] = []
    seen: set[str] = set()
    for raw in tags or []:
        if not isinstance(raw, str):
            continue
        # keep ':' so the repo:/topic: tag convention survives normalization
        t = re.sub(r"[^a-z0-9._/:-]+", "-", raw.strip().lower()).strip("-.")
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out
