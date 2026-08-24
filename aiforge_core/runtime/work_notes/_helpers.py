"""Leaf helpers, constants and regexes for the managed-note standard.

Dependency-free (no cross-group imports) so the rest of the package layers on
top without a cycle. See the package ``__init__`` for the full module docstring.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import re

_log = logging.getLogger("aiforge.work_notes")

# Canonical section order — the whole point of the standard. Never reorder.
# "Key Results" is title-cased per Google's OKR convention (whatmatters.com /
# re:Work): an Objective plus measurable Key Results.
_SECTION_ORDER = ("Objective", "Key Results", "Facts", "Links", "Learnings")
# Tolerant heading → canonical kwarg mapping (hand-edits vary in case/underscores).
_SECTION_KEYS = {
    "objective": "objective",
    "key results": "key_results",
    "key_results": "key_results",
    "facts": "facts",
    "links": "links",
    "learnings": "learnings",
}
_KEY_TO_HEADING = {
    "objective": "Objective", "key_results": "Key Results", "facts": "Facts",
    "links": "Links", "learnings": "Learnings",
}

# Each kind's primary note file — the target a cross-reference md link points at.
# jira/confluence write BOTH a raw source dump (ticket.md/page.md) AND a curated,
# OKR-enveloped merged note (dossier.md, see context_gather.gather). A cross-link
# must land on the CURATED dossier — the reader-facing note that carries the
# envelope + its own links — not the raw dump. web writes only page.md (its
# render_note note IS page.md, no separate merge) and repo only dossier.md.
_PRIMARY_NOTE = {"jira": "dossier.md", "confluence": "dossier.md",
                 "web": "page.md", "repo": "dossier.md"}

# LEGACY wiki-style cross-ref ([[jira/PROJ-42]]) — accepted on INPUT only and
# upgraded; the canonical output form is a relative md file link (_md_ref).
_WIKI_REF_RE = re.compile(r"^\[\[(jira|confluence|repo|web)/([^\]\s][^\]]*)\]\]$")
# Canonical cross-ref: a markdown link into a sibling context folder, e.g.
# [jira/PROJ-42](../../jira/PROJ-42/ticket.md). Notes live at
# work/<kind>/<key>/<file>.md, so ../../ walks to the work root.
_MD_REF_RE = re.compile(
    r"^\[[^\]]*\]\(\.\./\.\./(jira|confluence|repo|web)/([^/)\s]+)/[^)]*\.md\)$")
# Cross-SCOPE mapping between memory briefs. Briefs live FLAT in the memory dir
# (compacted-<scope>.md), so their cross-references are same-directory relative
# links to a sibling brief file. Optionally TYPED with a relationship prefix so
# the OKR Links section says HOW briefs relate, e.g.
#   depends-on: [time-sync](compacted-time-sync.md)
# The ``rel`` prefix is optional (a plain ``[x](compacted-x.md)`` still matches);
# named groups so callers read ``.group("file")`` / ``.group("rel")``.
_BRIEF_REF_RE = re.compile(
    r"^(?:(?P<rel>[a-z][a-z-]*):\s*)?"
    r"\[[^\]]*\]\((?P<file>compacted-[a-z0-9][a-z0-9-]*\.md)\)$")
# URL shapes that ARE managed dossiers (same regexes as work_context uses).
_JIRA_URL_RE = re.compile(r"/browse/([A-Z][A-Z0-9]{1,20}-\d+)\b")
_CONF_URL_RE = re.compile(r"(?:/pages/|pageId=)(\d{4,})")

# `[ \t]*` not `\s*`: `\s` MATCHES the newline, so `---\s*\n` could split a
# run of blank lines many ways — the super-linear case. What is actually
# meant is "trailing spaces/tabs on the --- line".
_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*\n?", re.DOTALL)

# OKF ⇄ legacy frontmatter key aliases. Writers emit the OKF name (left); the
# parser mirrors BOTH directions so a legacy on-disk file (kind/source_url/
# updated_at) and any legacy reader (fm["kind"]) keep working unchanged, while
# new OKF readers (fm["type"]) also resolve. type↔kind, resource↔source_url,
# timestamp↔updated_at.
_FM_ALIASES = (("type", "kind"), ("resource", "source_url"),
               ("timestamp", "updated_at"))


def _mirror_aliases(fm: dict) -> dict:
    """Fill each OKF/legacy key from its counterpart when only one is present,
    so both spellings resolve. Never overwrites an explicit value."""
    for okf, legacy in _FM_ALIASES:
        has_okf = str(fm.get(okf) or "").strip() != ""
        has_leg = str(fm.get(legacy) or "").strip() != ""
        if has_okf and not has_leg:
            fm[legacy] = fm[okf]
        elif has_leg and not has_okf:
            fm[okf] = fm[legacy]
    return fm

# Explicit start-of-body sentinel. Without it a free body following the last
# list section is indistinguishable from that section's items; an HTML comment
# is invisible in rendered markdown and survives hand-editing. parse_note
# still degrades gracefully (heuristic) when a hand-made file lacks it.
_BODY_MARK = "<!-- aiforge:body -->"


def _now_iso() -> str:
    """ISO-8601 UTC, second precision — stable-width, sortable, diff-friendly."""
    return _dt.datetime.now(_dt.UTC).replace(microsecond=0).isoformat()


def _yaml_str(v: str) -> str:
    """A YAML-safe scalar. json.dumps output is valid YAML and deterministic —
    no dependency on yaml.dump's flow/quote heuristics for WRITING (we still
    use yaml.safe_load for tolerant READING of hand-edited files)."""
    return json.dumps(str(v), ensure_ascii=False)


def _as_items(value) -> list[str]:
    """Coerce a section value to a clean list of item strings."""
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    out = []
    for v in value:
        s = str(v).strip()
        if s:
            out.append(s)
    return out
