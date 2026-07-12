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
import logging
import os
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
# (compacted-<scope>.md), not in the work/<kind>/<key>/ tree, so their
# cross-references are same-directory relative links to a sibling brief file,
# e.g. [global](compacted-shared.md). Kept verbatim (already canonical).
_BRIEF_REF_RE = re.compile(r"^\[[^\]]*\]\((compacted-[a-z0-9][a-z0-9-]*\.md)\)$")
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
        bm = _BRIEF_REF_RE.match(s)
        if mm:
            # re-canonicalize (label drift, filename drift) → stable dedupe
            canonical = _md_ref(mm.group(1), mm.group(2))
        elif wm:
            canonical = _md_ref(wm.group(1), wm.group(2))
        elif bm:
            canonical = s          # sibling-brief mapping link — already canonical
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


# ── Structure repair (write-path self-heal, never rejects) ────────────────
# Junk that a model (or a bad legacy fold) can leak INTO a Facts/Key results/
# Learnings item: the body sentinel, a markdown heading, a bare section label,
# a rule separator, or the brief's own Objective boilerplate. These aren't
# knowledge — they're the envelope scaffolding read back as content (the exact
# bug class we hit before). Scrubbed at render time so it never reaches disk.
_LEAK_ITEM_RE = re.compile(
    r"""^(?:
          \#{1,6}\s                                  # markdown heading
        | \#{0,2}\s*(?:objective|key\s*results|facts|links|learnings)\s*:?\s*$
        | -{3,}\s*$ | —{2,}\s*$                       # rule / separator
    )""", re.IGNORECASE | re.VERBOSE)
# Known envelope-boilerplate fragments that must never sit in a list item.
_BOILERPLATE_SUBSTR = ("keep durable, deduped knowledge",)


def _is_leak_item(s: str) -> bool:
    t = (s or "").strip()
    if not t or _BODY_MARK in t:
        return True
    if _LEAK_ITEM_RE.match(t):
        return True
    low = t.lower()
    return any(b in low for b in _BOILERPLATE_SUBSTR)


def scrub_items(value) -> list[str]:
    """`_as_items` + drop envelope-scaffolding leaks (heading/sentinel/section
    label/separator/boilerplate). Repair, not reject: bad items vanish, good
    ones stay."""
    return [s for s in _as_items(value) if not _is_leak_item(s)]


# Kinds this repo mints notes for; a blank/None kind is repaired to "knowledge"
# (the memory-brief default) rather than writing a header with no identity.
_KNOWN_KINDS = frozenset({"jira", "confluence", "web", "repo", "knowledge",
                          "topic", "session", "compacted", "rule", "prefs",
                          "note", "dossier"})


def validate_note(text: str) -> tuple[bool, list[str]]:
    """Re-parse a rendered note and report structure issues (does NOT mutate).
    Used by the write path to log what it repaired and by tests to assert the
    contract. ``ok`` is True when no issues remain."""
    issues: list[str] = []
    parsed = parse_note(text or "")
    fm = parsed.get("frontmatter") or {}
    for req in ("kind", "key", "updated_at"):
        if not str(fm.get(req) or "").strip():
            issues.append(f"missing frontmatter '{req}'")
    if not str(parsed.get("title") or "").strip():
        issues.append("missing title")
    sec = parsed.get("sections") or {}
    body = (parsed.get("body") or "").strip()
    has_content = bool((sec.get("objective") or "").strip()
                       or sec.get("facts") or sec.get("key_results")
                       or sec.get("learnings") or body)
    if not has_content:
        issues.append("empty note (no Objective/Facts/KR/Learnings/body)")
    for fld in ("facts", "key_results", "learnings"):
        for it in sec.get(fld) or []:
            if _is_leak_item(it):
                issues.append(f"scaffolding leaked into {fld}: {it[:40]!r}")
    return (not issues), issues


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


def render_note(kind: str, key: str, *, title: str, source_url: str = "",
                objective: str = "", key_results=None, facts=None,
                links=None, learnings=None, body_md: str = "",
                updated_at: str = "", tags=None) -> str:
    """Render the standard note. Empty sections are skipped; ordering is fixed
    (frontmatter → title → Objective → Key Results → Facts → Links → Learnings
    → free body). ``updated_at`` is injectable for deterministic tests /
    read-modify-write; it defaults to now (UTC). ``tags`` land in the
    frontmatter (metadata, not a body section)."""
    # Repair (never reject): a blank kind gets the memory-brief default so the
    # header always has an identity; unknown kinds pass through (repo mints new
    # ones over time — don't gate on an allow-list).
    kind = (str(kind or "").strip() or "knowledge")
    norm_links = normalize_links(links, kind, key)
    norm_tags = normalize_tags(tags)
    fm = [
        "---",
        f"kind: {_yaml_str(kind)}",
        f"key: {_yaml_str(key)}",
        f"source_url: {_yaml_str(source_url or '')}",
        f"updated_at: {_yaml_str(updated_at or _now_iso())}",
    ]
    if norm_tags:
        fm.append("tags:")
        fm.extend(f"  - {_yaml_str(t)}" for t in norm_tags)
    else:
        fm.append("tags: []")
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
    # scrub_items drops envelope scaffolding (heading/sentinel/section-label/
    # boilerplate) that a model or bad fold leaked into a list section.
    for heading, items in (("Key Results", scrub_items(key_results)),
                           ("Facts", scrub_items(facts)),
                           ("Links", norm_links),
                           ("Learnings", scrub_items(learnings))):
        if items:
            parts.append(f"## {heading}\n\n"
                         + "\n".join(f"- {i}" for i in items))
    body = (body_md or "").strip("\n")
    if body:
        parts.append(_BODY_MARK + "\n\n" + body)
    text = "\n\n".join(parts) + "\n"
    # Safety net: re-parse and log anything the scrub couldn't fix (never
    # raises — write proceeds; the log surfaces a real structural regression).
    ok, issues = validate_note(text)
    if not ok:
        _log.warning("render_note[%s/%s] structure issues: %s", kind, key, issues)
    return text


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
        tags=_pick("tags", fm.get("tags")),
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
    # Learnings — discoveries, gotchas, dated changes — are the HIGHEST-signal
    # recall content; surface them too (they were dropped before, so a model
    # never saw a brief's gotchas). Objective boilerplate + title + sentinel
    # stay stripped (that's the scaffolding this projection exists to remove).
    learnings = sec.get("learnings") or []
    if learnings:
        out.append("\n".join(f"- {l}" for l in learnings))
    if parsed.get("body"):
        out.append(parsed["body"].strip())
    return "\n\n".join(out).strip() or (note or "").strip()


# ── intelligent consolidation (LLM map+dedupe → OKR sections) ─────────────
#
# update_note is a DUMB whole-section replace: the caller must hand it the full
# merged list or it clobbers. consolidate() is the smart write — it folds NEW
# free-form knowledge INTO the existing OKR sections with an LLM that dedupes
# paraphrases, resolves contradictions (newer supersedes), and MAPS each item to
# the right section. Large input is cut on STRUCTURE boundaries (chonkie) and
# folded chunk-by-chunk so nothing is sliced mid-fact. Soft: any LLM/adapter
# failure degrades to a deterministic union+dedupe merge — never raises, never
# loses the new content.

def _okf_rules() -> str:
    """The OKF v0.1 producer rules (single source: memory.okf) — appended to the
    consolidation prompt so the compacted note stays an OKF-valid concept."""
    try:
        from aiforge_core.memory.okf import OKF_RULES
        return OKF_RULES
    except Exception:  # noqa: BLE001
        return ""


def _supersede_directive() -> str:
    """The contradiction-handling rule for consolidation — config-driven via
    ``AIFORGE_OKR_SUPERSEDE`` (``archive`` | ``keep``). ``archive`` (default)
    drops the stale line (OKR cycle-close; git history keeps the old value);
    ``keep`` tags it ``[superseded <date>]`` and keeps both (a visible
    retrospective trail in the brief). Read per call so the env is live."""
    if os.environ.get("AIFORGE_OKR_SUPERSEDE", "archive").strip().lower() == "keep":
        today = _dt.datetime.now(_dt.UTC).date().isoformat()
        return (f"SUPERSEDE: when new info contradicts an old line, KEEP BOTH — "
                f"append ' [superseded {today}]' to the stale line and add the "
                f"new value as a fresh line; never delete the old value.")
    return ("SUPERSEDE: when new info contradicts an old line (status/owner/value "
            "changed), keep the NEW value and DROP the stale line.")


_CONSOLIDATE_SYS = (
    "You maintain a knowledge note in Google-OKR format. You are given the "
    "note's CURRENT sections (JSON) and NEW information. Produce the CONSOLIDATED "
    "sections.\n"
    "Rules:\n"
    "- DEDUPE: merge paraphrases/near-duplicates into one crisp line; never emit "
    "two lines saying the same thing.\n"
    "- MAP each item to the correct section: Objective = the one-line goal; "
    "Key Results = measurable outcomes/targets AND tickets worked — a jira/issue "
    "key (e.g. PROJ-123) IS a Key Result (the concrete measurable work) and its "
    "reference is ALSO copied into Links; Facts = stable truths, config, points "
    "to remember, current state; Links = URLs / cross-references (COPY VERBATIM, "
    "never reword or invent); Learnings = discoveries, gotchas, dated changes.\n"
    "- Keep every item ONE concise sentence. Do NOT invent facts not present in "
    "the inputs. Preserve existing content unless a rule above removes it.\n"
    "\n"
    + _okf_rules()
)


def _ci_key(s: str) -> str:
    """Case/space-insensitive dedupe key for a section item."""
    return re.sub(r"\s+", " ", str(s or "").strip().lower())


_JUNK_ITEM_RE = re.compile(
    r"^(?:#{1,6}\s|-{3,}\s*$|_source:|_gathered\b|```|<!--)", re.I)


def _dedupe_ci(items) -> list[str]:
    """Order-preserving dedupe of a section's items. Beyond exact (case/space-
    insensitive) dupes it drops a shorter item fully CONTAINED in a longer kept
    one (the common near-dupe: "status: Done" vs "status: Done (auto)") and
    strips obvious junk lines (markdown headers/rules/fences, source markers)
    that leak in when a raw blob is folded without an LLM."""
    cleaned: list[str] = []
    seen: set[str] = set()
    for it in _as_items(items):
        s = str(it).strip()
        if not s or _JUNK_ITEM_RE.match(s):
            continue
        k = _ci_key(s)
        if k and k not in seen:
            seen.add(k)
            cleaned.append(s)
    # containment pass: drop any item whose text is a substring of a longer one
    out: list[str] = []
    keys = [_ci_key(c) for c in cleaned]
    for i, c in enumerate(cleaned):
        ki = keys[i]
        if any(i != j and ki in keys[j] and len(keys[j]) > len(ki)
               for j in range(len(cleaned))):
            continue
        out.append(c)
    return out


def _sections_dict(objective="", key_results=None, facts=None, links=None,
                   learnings=None) -> dict:
    return {"objective": (objective or "").strip(),
            "key_results": _as_items(key_results), "facts": _as_items(facts),
            "links": _as_items(links), "learnings": _as_items(learnings)}


def _deterministic_merge(existing: dict, new_content: str) -> dict:
    """No-LLM fallback: append the new content as a single deduped Fact and
    dedupe every section. Never loses information, never reorders history."""
    out = {
        "objective": (existing.get("objective") or "").strip(),
        "key_results": _dedupe_ci(existing.get("key_results")),
        "facts": _dedupe_ci(existing.get("facts")),
        "links": _dedupe_ci(existing.get("links")),
        "learnings": _dedupe_ci(existing.get("learnings")),
    }
    fact = re.sub(r"\s+", " ", (new_content or "").strip())
    if fact and _ci_key(fact) not in {_ci_key(f) for f in out["facts"]}:
        out["facts"].append(fact)
    return out


def _consolidate_once(existing: dict, new_content: str, role: str) -> dict:
    """One structured LLM fold: (existing sections + new_content) → consolidated
    sections. Returns the deterministic merge on ANY failure."""
    from pydantic import BaseModel

    class ConsolidatedNote(BaseModel):
        objective: str = ""
        key_results: list[str] = []
        facts: list[str] = []
        links: list[str] = []
        learnings: list[str] = []

    payload = json.dumps({"current_sections": existing,
                          "new_information": new_content}, ensure_ascii=False)
    try:
        import os as _os
        from aiforge_core.llm.structured import structured_complete
        # The consolidated JSON re-emits EVERY accumulated fact each fold, so a
        # fixed output cap truncates a fact-heavy brief (IncompleteOutputException
        # → fallback loop). A dedupe-fold never EXPANDS its input, so size the
        # output budget from the actual payload (≈chars/3 tokens + slack), clamped
        # to a ceiling. Dynamic, not a magic constant; override the ceiling with
        # AIFORGE_CONSOLIDATE_MAX_TOKENS.
        _cap = int(_os.environ.get("AIFORGE_CONSOLIDATE_MAX_TOKENS", "32768"))
        _mt = max(4096, min(_cap, len(payload) // 3 + 1024))
        # Inject the supersede directive at call time (env is live) so the
        # contradiction policy (archive vs keep) is honoured per run.
        sys_prompt = _CONSOLIDATE_SYS + "\n- " + _supersede_directive()
        res = structured_complete(
            role,
            [{"role": "system", "content": sys_prompt},
             {"role": "user", "content": payload}],
            ConsolidatedNote, max_retries=1, max_tokens=_mt, temperature=0.1)
        return {"objective": (res.objective or "").strip(),
                "key_results": _dedupe_ci(res.key_results),
                "facts": _dedupe_ci(res.facts),
                # links pass through normalize_links at render time; dedupe here
                "links": _dedupe_ci(res.links),
                "learnings": _dedupe_ci(res.learnings)}
    except Exception:  # noqa: BLE001 — model down / bad JSON → deterministic
        return _deterministic_merge(existing, new_content)


def consolidate(existing: dict, new_content: str, *, role: str = "learner",
                max_input_chars: int | None = None) -> dict:
    """Fold ``new_content`` into ``existing`` OKR sections via an LLM that
    dedupes, resolves contradictions, and maps each item to its section.

    ``existing`` is a sections dict (objective:str, the rest lists — missing
    keys tolerated). Large ``new_content`` is cut on STRUCTURE boundaries
    (chonkie) and folded chunk-by-chunk. Returns a consolidated sections dict;
    degrades to a deterministic union+dedupe merge if no model is reachable.

    ``max_input_chars`` is the total per-call input window (existing JSON + one
    chunk). Defaults from AIFORGE_CONSOLIDATE_INPUT_CHARS (48000) — modern
    long-context models swallow that whole, and a conservative 12k window made
    a large brief collapse to a 1k budget → dozens of tiny chunks (slow + poor
    distillation)."""
    import os as _os
    if max_input_chars is None:
        max_input_chars = int(
            _os.environ.get("AIFORGE_CONSOLIDATE_INPUT_CHARS", "48000"))
    cur = _sections_dict(**{k: existing.get(k) for k in
                            ("objective", "key_results", "facts", "links",
                             "learnings") if k in existing}) \
        if existing else _sections_dict()
    text = (new_content or "").strip()
    if not text:
        # nothing new — just normalize/dedupe the existing sections (no LLM)
        return {"objective": cur["objective"],
                "key_results": _dedupe_ci(cur["key_results"]),
                "facts": _dedupe_ci(cur["facts"]),
                "links": _dedupe_ci(cur["links"]),
                "learnings": _dedupe_ci(cur["learnings"])}

    # Budget the per-call input: reserve room for the existing sections JSON.
    reserve = len(json.dumps(cur, ensure_ascii=False))
    # Never collapse to a sliver: a big existing brief must still get a usable
    # chunk budget (else 25k of new text becomes 27 folds). Floor at 8k.
    budget = max(8000, max_input_chars - reserve)
    chunks: list[str]
    if len(text) <= budget:
        chunks = [text]
    else:
        try:
            from aiforge_core.integrations import chonkie_text_adapter as _ck
            if _ck.available():
                # structure-aware chunks under budget; fold each in turn
                chunks = []
                buf, used = [], 0
                for part in _ck.chunk_text(text, chunk_tokens=max(64, budget // 8)):
                    if used and used + len(part) > budget:
                        chunks.append("".join(buf))
                        buf, used = [], 0
                    buf.append(part)
                    used += len(part)
                if buf:
                    chunks.append("".join(buf))
            else:
                chunks = [text[i:i + budget] for i in range(0, len(text), budget)]
        except Exception:  # noqa: BLE001 — chunker down → plain slices
            chunks = [text[i:i + budget] for i in range(0, len(text), budget)]

    if len(chunks) > 1:
        _log.info("consolidate: folding %d chunk(s) (%d chars) via LLM…",
                  len(chunks), len(text))
    for _ci, ch in enumerate(chunks, 1):
        if len(chunks) > 1:
            _log.info("consolidate: chunk %d/%d (%d chars)…", _ci, len(chunks), len(ch))
        cur = _consolidate_once(cur, ch, role)
    return cur


def consolidate_note(path: str, new_content: str, *, role: str = "learner",
                     kind: str | None = None, key: str | None = None) -> dict:
    """Read-modify-write ``path``: intelligently fold ``new_content`` into the
    note's OKR sections (LLM map+dedupe, chonkie for big input) and rewrite in
    OKR format — preserving unknown sections + free body. Soft-error contract:
    returns ``{"ok", ...}``, never raises."""
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        return {"ok": False, "error": f"read failed: {exc}"}
    parsed = parse_note(text)
    fm, sec = parsed["frontmatter"], parsed["sections"]
    k = str(kind or fm.get("kind") or "misc")
    key_ = str(key or fm.get("key") or "unknown")
    merged = consolidate(sec, new_content, role=role)
    rendered = render_note(
        k, key_, title=parsed["title"] or key_,
        source_url=str(fm.get("source_url") or ""),
        objective=merged["objective"], key_results=merged["key_results"],
        facts=merged["facts"], links=merged["links"],
        learnings=merged["learnings"], body_md=parsed["body"],
        updated_at=_now_iso(), tags=fm.get("tags"))
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


__all__ = ["render_note", "parse_note", "normalize_links", "update_note",
           "knowledge_text", "consolidate", "consolidate_note"]
