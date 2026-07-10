"""The ONE standard format for managed workspace notes.

Every markdown artifact the system writes into a shared work-context folder
(jira ticket.md/dossier.md, confluence page.md, web page.md — see
``work_context``) had its own ad-hoc shape. This module is the single
renderer/parser for all of them:

    ---                       ← YAML frontmatter (machine-readable identity)
    kind: jira
    key: PROJ-42
    source_url: https://jira/browse/PROJ-42
    updated_at: 2026-07-10T00:00:00+00:00
    links:
      - "https://jira/browse/PROJ-42"
      - "[confluence/12345](../../confluence/12345/page.md)"
    ---
    # PROJ-42 — the title

    ## Objective                ← Google-OKR sections, fixed order:
    ## Key Results                an Objective + measurable Key Results
    ## Facts                      (Google's structure); Facts/Links/
    ## Links                      Learnings extend it for dossier upkeep
    ## Learnings
    <free body — the full page/ticket text, preserved verbatim>

Cross-references to OTHER managed dossiers are stored as RELATIVE MARKDOWN
FILE LINKS to the target note's md file
(``[jira/PROJ-42](../../jira/PROJ-42/ticket.md)``) — they render/click as
plain markdown, survive a Jira/Confluence base-URL change, and the curator can
resolve them locally. Legacy ``[[kind/key]]`` wiki refs are still accepted on
input and upgraded to md links on the next write. Rendering is DETERMINISTIC
(stable section order, stable link order) so repeated writes of the same data
produce byte-identical files — the git-ignored dossier folders never look
"changed" from a re-render.

Soft-error contract: ``update_note`` returns ``{"ok": bool, ...}`` and never
raises; ``parse_note`` is tolerant of hand-edited / legacy files (missing
frontmatter, unknown sections) and never raises on str input.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re

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
_PRIMARY_NOTE = {"jira": "ticket.md", "confluence": "page.md",
                 "web": "page.md", "repo": "dossier.md"}

# LEGACY wiki-style cross-ref ([[jira/PROJ-42]]) — accepted on INPUT only and
# upgraded; the canonical output form is a relative md file link (_md_ref).
_WIKI_REF_RE = re.compile(r"^\[\[(jira|confluence|repo|web)/([^\]\s][^\]]*)\]\]$")
# Canonical cross-ref: a markdown link into a sibling context folder, e.g.
# [jira/PROJ-42](../../jira/PROJ-42/ticket.md). Notes live at
# work/<kind>/<key>/<file>.md, so ../../ walks to the work root.
_MD_REF_RE = re.compile(
    r"^\[[^\]]*\]\(\.\./\.\./(jira|confluence|repo|web)/([^/)\s]+)/[^)]*\.md\)$")
# URL shapes that ARE managed dossiers (same regexes as work_context uses).
_JIRA_URL_RE = re.compile(r"/browse/([A-Z][A-Z0-9]{1,20}-\d+)\b")
_CONF_URL_RE = re.compile(r"(?:/pages/|pageId=)(\d{4,})")

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)

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


def _md_ref(ref_kind: str, ref_key: str) -> str:
    """The canonical cross-reference: a relative markdown link to the target
    dossier's primary md file. The path segment uses work_context's slug so
    the link resolves to the folder that ACTUALLY exists on disk."""
    from aiforge_core.runtime import work_context
    slug = work_context._slug(str(ref_key))
    return (f"[{ref_kind}/{ref_key}]"
            f"(../../{ref_kind}/{slug}/{_PRIMARY_NOTE.get(ref_kind, 'dossier.md')})")


def normalize_links(links, kind: str, key: str) -> list[str]:
    """Canonicalize a link list for a ``(kind, key)`` note.

    - only http(s) URLs pass the scheme filter (a persisted note is shared
      state — file:///javascript: etc. must never land in it);
    - a reference to ANOTHER managed dossier — a /browse/KEY-123 or
      /pages/<id> URL, a legacy ``[[kind/key]]`` wiki ref, or an existing md
      ref — becomes the canonical relative MARKDOWN FILE LINK
      ``[kind/key](../../kind/key/ticket.md)``; the note's OWN canonical URL
      stays a URL (it IS the source link);
    - everything is deduped, order preserved (first occurrence wins) so
      output is deterministic.
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in links or []:
        if not isinstance(raw, str):
            continue
        s = raw.strip()
        if not s:
            continue
        mm = _MD_REF_RE.match(s)
        wm = _WIKI_REF_RE.match(s)
        if mm:
            # re-canonicalize (label drift, filename drift) → stable dedupe
            canonical = _md_ref(mm.group(1), mm.group(2))
        elif wm:
            canonical = _md_ref(wm.group(1), wm.group(2))
        else:
            if not re.match(r"^https?://", s, re.IGNORECASE):
                continue                      # scheme filter — http(s) only
            jm = _JIRA_URL_RE.search(s)
            cm = _CONF_URL_RE.search(s)
            if jm and not (kind == "jira" and jm.group(1) == str(key)):
                canonical = _md_ref("jira", jm.group(1))
            elif cm and not (kind == "confluence" and cm.group(1) == str(key)):
                canonical = _md_ref("confluence", cm.group(1))
            else:
                canonical = s
        if canonical not in seen:
            seen.add(canonical)
            out.append(canonical)
    return out


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


def render_note(kind: str, key: str, *, title: str, source_url: str = "",
                objective: str = "", key_results=None, facts=None,
                links=None, learnings=None, body_md: str = "",
                updated_at: str = "") -> str:
    """Render the standard note. Empty sections are skipped; ordering is fixed
    (frontmatter → title → Objective → Key Results → Facts → Links → Learnings
    → free body). ``updated_at`` is injectable for deterministic tests /
    read-modify-write; it defaults to now (UTC)."""
    norm_links = normalize_links(links, kind, key)
    fm = [
        "---",
        f"kind: {_yaml_str(kind)}",
        f"key: {_yaml_str(key)}",
        f"source_url: {_yaml_str(source_url or '')}",
        f"updated_at: {_yaml_str(updated_at or _now_iso())}",
    ]
    if norm_links:
        fm.append("links:")
        fm.extend(f"  - {_yaml_str(lk)}" for lk in norm_links)
    else:
        fm.append("links: []")
    fm.append("---")

    parts = ["\n".join(fm), f"# {str(title or key).strip()}"]
    obj = (objective or "").strip()
    if obj:
        parts.append("## Objective\n\n" + obj)
    for heading, items in (("Key Results", _as_items(key_results)),
                           ("Facts", _as_items(facts)),
                           ("Links", norm_links),
                           ("Learnings", _as_items(learnings))):
        if items:
            parts.append(f"## {heading}\n\n"
                         + "\n".join(f"- {i}" for i in items))
    body = (body_md or "").strip("\n")
    if body:
        parts.append(_BODY_MARK + "\n\n" + body)
    return "\n\n".join(parts) + "\n"


def parse_note(text: str) -> dict:
    """Parse a note (tolerantly) into
    ``{"frontmatter", "title", "sections", "body"}``.

    - ``sections`` holds ONLY the known OKR sections (canonical keys:
      objective → str; key_results/facts/links/learnings → list of items).
    - Everything else — prose outside the known sections and any UNKNOWN
      ``## Heading`` blocks (hand-added by users) — lands in ``body`` in
      original order, so a read-modify-write round-trip preserves it.
    Never raises on a str input; a legacy/ad-hoc file simply parses as
    frontmatter={} with everything in body.
    """
    text = text or ""
    fm: dict = {}
    m = _FRONTMATTER_RE.match(text)
    if m:
        try:
            import yaml
            loaded = yaml.safe_load(m.group(1))
            if isinstance(loaded, dict):
                fm = loaded
        except Exception:  # noqa: BLE001 — hand-edited YAML must not explode
            fm = {}
        text = text[m.end():]

    # The explicit body sentinel splits managed head from free body exactly.
    # Hand-made files without it fall through to the heuristic below (a
    # non-bullet line ends a list section).
    lines = text.splitlines()
    tail = ""
    for i, ln in enumerate(lines):
        if ln.strip() == _BODY_MARK:
            tail = "\n".join(lines[i + 1:]).strip("\n")
            lines = lines[:i]
            break

    title = ""
    sections: dict = {}
    body_parts: list[str] = []
    current: str | None = None      # canonical key of the OKR section being read
    current_lines: list[str] = []
    unknown_lines: list[str] = []   # accumulates non-section prose/unknown blocks

    def _flush_section():
        nonlocal current, current_lines
        if current is None:
            return
        chunk = "\n".join(current_lines).strip("\n")
        if current == "objective":
            sections["objective"] = chunk.strip()
        else:
            items = [re.sub(r"^[-*]\s+", "", ln.strip())
                     for ln in chunk.splitlines() if ln.strip()]
            sections[current] = items
        current, current_lines = None, []

    for line in lines:
        if line.startswith("# ") and not title and current is None \
                and not any(s.strip() for s in unknown_lines):
            title = line[2:].strip()
            continue
        hm = re.match(r"^##\s+(.+?)\s*$", line)
        if hm:
            canon = _SECTION_KEYS.get(hm.group(1).strip().lower())
            _flush_section()
            if canon:
                current = canon
                continue
            # unknown ## section → preserved verbatim in body
            unknown_lines.append(line)
            continue
        # Heuristic terminator (marker-less hand edits): a non-blank,
        # non-bullet line inside a LIST section means the section is over
        # and free body has begun.
        if current is not None and current != "objective" and line.strip() \
                and not line.lstrip().startswith(("-", "*")):
            _flush_section()
            unknown_lines.append(line)
            continue
        if current is not None:
            current_lines.append(line)
        else:
            unknown_lines.append(line)
    _flush_section()
    if any(s.strip() for s in unknown_lines):
        body_parts.append("\n".join(unknown_lines).strip("\n"))
    if tail:
        body_parts.append(tail)

    return {"frontmatter": fm, "title": title, "sections": sections,
            "body": "\n\n".join(body_parts)}


def update_note(path: str, **section_updates) -> dict:
    """Read-modify-write a note in place: apply the given section updates
    (``objective``/``key_results``/``facts``/``links``/``learnings``/``title``/
    ``source_url``/``body_md``), preserve everything else (unknown sections and
    free body survive via parse_note), bump ``updated_at``, and write
    atomically (tmp + rename — a concurrent reader never sees a torn note).
    Soft-error: returns ``{"ok": bool, ...}``, never raises."""
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        return {"ok": False, "error": f"read failed: {exc}"}
    parsed = parse_note(text)
    fm = parsed["frontmatter"]
    sec = parsed["sections"]

    kind = str(section_updates.pop("kind", None) or fm.get("kind") or "misc")
    key = str(section_updates.pop("key", None) or fm.get("key") or "unknown")

    def _pick(name, current):
        return section_updates.get(name, current)

    rendered = render_note(
        kind, key,
        title=_pick("title", parsed["title"] or key),
        source_url=str(_pick("source_url", fm.get("source_url") or "")),
        objective=_pick("objective", sec.get("objective", "")),
        key_results=_pick("key_results", sec.get("key_results")),
        facts=_pick("facts", sec.get("facts")),
        links=_pick("links", sec.get("links")),
        learnings=_pick("learnings", sec.get("learnings")),
        body_md=_pick("body_md", parsed["body"]),
        updated_at=_now_iso(),      # the write IS the freshness event
    )
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(rendered)
        os.replace(tmp, path)
    except OSError as exc:
        import contextlib
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        return {"ok": False, "error": f"write failed: {exc}"}
    return {"ok": True, "path": path}


def knowledge_text(note: str) -> str:
    """The KNOWLEDGE content of a note for injection/recall — its Facts +
    consolidated free body — WITHOUT the envelope's own metadata (the
    ``## Objective`` boilerplate that describes what the file is, the title, the
    ``<!-- aiforge:body -->`` sentinel). Use this wherever a note is fed to a
    model as context (auto-injected briefs, recall ingest) so the machine
    scaffolding never reads as a project fact. A legacy/plain note with no OKR
    sections degrades to its whole body. Accepts a full note OR a
    frontmatter-stripped body (``md_store.read_file`` returns the latter)."""
    parsed = parse_note(note or "")
    sec = parsed.get("sections") or {}
    out: list[str] = []
    # measurable Key Results (a repo-note's scan counts) THEN Facts — both are
    # knowledge; a memory brief only fills Facts, a repo-note only Key Results.
    krs = sec.get("key_results") or []
    if krs:
        out.append("\n".join(f"- {k}" for k in krs))
    facts = sec.get("facts") or []
    if facts:
        out.append("\n".join(f"- {f}" for f in facts))
    if parsed.get("body"):
        out.append(parsed["body"].strip())
    return "\n\n".join(out).strip() or (note or "").strip()


__all__ = ["render_note", "parse_note", "normalize_links", "update_note",
           "knowledge_text"]
