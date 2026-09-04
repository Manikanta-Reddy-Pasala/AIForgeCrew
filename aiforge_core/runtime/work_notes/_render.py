"""Render / parse / validate / update the standard OKR note (no LLM)."""
from __future__ import annotations

import os
import re

from aiforge_core.config import _atomic

from ._helpers import (
    _BODY_MARK,
    _FRONTMATTER_RE,
    _SECTION_KEYS,
    _as_items,
    _log,
    _mirror_aliases,
    _now_iso,
    _yaml_str,
)
from ._links import normalize_links, normalize_tags


# ── Structure repair (write-path self-heal, never rejects) ────────────────
# Junk that a model (or a bad legacy fold) can leak INTO a Facts/Key results/
# Learnings item: the body sentinel, a markdown heading, a bare section label,
# a rule separator, or the brief's own Objective boilerplate. These aren't
# knowledge — they're the envelope scaffolding read back as content (the exact
# bug class we hit before). Scrubbed at render time so it never reaches disk.
# Three separate patterns rather than one alternation of four: they recognise
# unrelated things, and as a single expression it was complex enough that
# changing one branch meant re-reading all of them.
_LEAK_HEADING_RE = re.compile(r"^\#{1,6}\s")
_LEAK_LABEL_RE = re.compile(
    r"^\#{0,2}\s*(?:objective|key\s*results|facts|links|learnings)\s*+:?\s*+$",
    re.IGNORECASE)
_LEAK_RULE_RE = re.compile(r"^(?:-{3,}|—{2,})\s*+$")
_LEAK_PATTERNS = (_LEAK_HEADING_RE, _LEAK_LABEL_RE, _LEAK_RULE_RE)
# Known envelope-boilerplate fragments that must never sit in a list item.
_BOILERPLATE_SUBSTR = ("keep durable, deduped knowledge",)


def _is_leak_item(s: str) -> bool:
    t = (s or "").strip()
    if not t or _BODY_MARK in t:
        return True
    if any(rx.match(t) for rx in _LEAK_PATTERNS):
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


def _frontmatter_issues(fm: dict) -> list[str]:
    """OKF requires a non-empty ``type:``; we additionally want ``key`` and a
    ``timestamp``. parse_note mirrors legacy aliases so an old file (kind /
    updated_at) still satisfies these."""
    return [f"missing frontmatter '{r}'" for r in ("type", "key", "timestamp")
            if not str(fm.get(r) or "").strip()]


def _content_issues(sec: dict, body: str) -> list[str]:
    has_content = bool((sec.get("objective") or "").strip()
                       or sec.get("facts") or sec.get("key_results")
                       or sec.get("learnings") or body)
    if has_content:
        return []
    return ["empty note (no Objective/Facts/KR/Learnings/body)"]


def _leak_issues(sec: dict) -> list[str]:
    return [f"scaffolding leaked into {fld}: {it[:40]!r}"
            for fld in ("facts", "key_results", "learnings")
            for it in (sec.get(fld) or []) if _is_leak_item(it)]


def validate_note(text: str) -> tuple[bool, list[str]]:
    """Re-parse a rendered note and report structure issues (does NOT mutate).
    Used by the write path to log what it repaired and by tests to assert the
    contract. ``ok`` is True when no issues remain."""
    parsed = parse_note(text or "")
    sec = parsed.get("sections") or {}
    issues = _frontmatter_issues(parsed.get("frontmatter") or {})
    if not str(parsed.get("title") or "").strip():
        issues.append("missing title")
    issues += _content_issues(sec, (parsed.get("body") or "").strip())
    issues += _leak_issues(sec)
    return (not issues), issues


def _yaml_list(name: str, values: list, *, empty_ok: bool = True) -> list[str]:
    """``name:`` block, or ``name: []`` when empty and that form is wanted."""
    if values:
        return [f"{name}:"] + [f"  - {_yaml_str(v)}" for v in values]
    return [f"{name}: []"] if empty_ok else []


def _note_frontmatter(kind: str, key: str, res: str, ts: str, desc: str,
                      norm_tags: list, norm_links: list,
                      norm_sources: list) -> list[str]:
    fm = ["---", f"type: {_yaml_str(kind)}", f"key: {_yaml_str(key)}",
          f"resource: {_yaml_str(res)}", f"timestamp: {_yaml_str(ts)}"]
    if desc:
        fm.append(f"description: {_yaml_str(desc)}")
    fm += _yaml_list("tags", norm_tags)
    fm += _yaml_list("links", norm_links)
    # `sources` is emitted only when non-empty, so notes that consume nothing
    # keep the frontmatter they always had.
    fm += _yaml_list("sources", norm_sources, empty_ok=False)
    fm.append("---")
    return fm


def render_note(kind: str, key: str, *, title: str, source_url: str = "",
                objective: str = "", key_results=None, facts=None,
                links=None, learnings=None, body_md: str = "",
                updated_at: str = "", tags=None,
                description: str = "", resource: str = "",
                timestamp: str = "", sources=None) -> str:
    """Render the standard note in **Open Knowledge Format (OKF v0.1)**: the
    frontmatter's required identity field is ``type:`` (from ``kind``), the
    resource URI is ``resource:`` (from ``resource``/``source_url``), the
    last-change stamp is ``timestamp:`` (from ``timestamp``/``updated_at``), and
    an optional one-line ``description:`` is emitted when given. Empty sections
    are skipped; ordering is fixed (frontmatter → title → Objective → Key
    Results → Facts → Links → Learnings → free body). The stamp is injectable
    for deterministic tests / read-modify-write; it defaults to now (UTC).
    ``tags`` land in the frontmatter (metadata, not a body section), and so does
    ``sources`` — the PROVENANCE of a folded note: the stems of the raw capture
    files whose content this note now carries. Emitted only when non-empty, so
    notes that consume nothing keep the frontmatter they always had.

    Legacy aliases (``source_url``/``updated_at`` kwargs) are still accepted so
    callers need no change; the OUTPUT is always OKF names. :func:`parse_note`
    mirrors OKF↔legacy keys so old files and old readers keep working."""
    # Repair (never reject): a blank kind gets the memory-brief default so the
    # header always has an identity; unknown kinds pass through (repo mints new
    # ones over time — don't gate on an allow-list).
    kind = (str(kind or "").strip() or "knowledge")
    norm_links = normalize_links(links, kind, key)
    fm = _note_frontmatter(
        kind, key, (resource or source_url or "").strip(),
        (timestamp or updated_at or _now_iso()), (description or "").strip(),
        normalize_tags(tags), norm_links,
        [s for s in (str(x).strip() for x in (sources or [])) if s])

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


def _split_frontmatter(text: str) -> tuple[dict, str]:
    """``(frontmatter, rest)``. Hand-edited YAML must not explode."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    fm: dict = {}
    try:
        import yaml
        loaded = yaml.safe_load(m.group(1))
        if isinstance(loaded, dict):
            fm = _mirror_aliases(loaded)
    except Exception:  # noqa: BLE001
        fm = {}
    return fm, text[m.end():]


def _split_body_sentinel(text: str) -> tuple[list[str], str]:
    """``(head lines, free tail)``. The explicit body sentinel splits managed
    head from free body exactly; hand-made files without it fall through to the
    heuristic in the reader (a non-bullet line ends a list section)."""
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if ln.strip() == _BODY_MARK:
            return lines[:i], "\n".join(lines[i + 1:]).strip("\n")
    return lines, ""


class _NoteReader:
    """Walks a note's head lines into the known OKR sections, keeping
    everything else (prose, unknown ``## Heading`` blocks) in original order so
    a read-modify-write round-trip preserves it."""

    __slots__ = ("title", "sections", "current", "current_lines", "unknown")

    def __init__(self) -> None:
        self.title = ""
        self.sections: dict = {}
        self.current: str | None = None   # canonical key being read
        self.current_lines: list[str] = []
        self.unknown: list[str] = []      # non-section prose / unknown blocks

    def _flush(self) -> None:
        if self.current is None:
            return
        chunk = "\n".join(self.current_lines).strip("\n")
        if self.current == "objective":
            self.sections["objective"] = chunk.strip()
        else:
            self.sections[self.current] = [
                re.sub(r"^[-*]\s+", "", ln.strip())
                for ln in chunk.splitlines() if ln.strip()]
        self.current, self.current_lines = None, []

    def _take_title(self, line: str) -> bool:
        if (line.startswith("# ") and not self.title and self.current is None
                and not any(s.strip() for s in self.unknown)):
            self.title = line[2:].strip()
            return True
        return False

    def _take_heading(self, line: str) -> bool:
        # A prefix test and a strip, not a quantifier over the line: same
        # answer, and no denial-of-service question about note text.
        if not line.startswith("##") or line[2:3] not in (" ", "\t"):
            return False
        title = line[2:].strip()
        if not title:
            return False
        canon = _SECTION_KEYS.get(title.lower())
        self._flush()
        if canon:
            self.current = canon
        else:
            self.unknown.append(line)   # unknown ## section → verbatim in body
        return True

    def _ends_list_section(self, line: str) -> bool:
        """Heuristic terminator (marker-less hand edits): a non-blank,
        non-bullet line inside a LIST section means the section is over and
        free body has begun."""
        return (self.current is not None and self.current != "objective"
                and bool(line.strip())
                and not line.lstrip().startswith(("-", "*")))

    def feed(self, line: str) -> None:
        if self._take_title(line) or self._take_heading(line):
            return
        if self._ends_list_section(line):
            self._flush()
            self.unknown.append(line)
        elif self.current is not None:
            self.current_lines.append(line)
        else:
            self.unknown.append(line)

    def finish(self) -> None:
        self._flush()


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
    fm, rest = _split_frontmatter(text or "")
    lines, tail = _split_body_sentinel(rest)
    reader = _NoteReader()
    for line in lines:
        reader.feed(line)
    reader.finish()
    body_parts: list[str] = []
    if any(s.strip() for s in reader.unknown):
        body_parts.append("\n".join(reader.unknown).strip("\n"))
    if tail:
        body_parts.append(tail)
    return {"frontmatter": fm, "title": reader.title,
            "sections": reader.sections, "body": "\n\n".join(body_parts)}


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

    # fm carries mirrored OKF+legacy keys (parse_note); accept either spelling
    # for the identity/resource/description fields on update.
    kind = str(section_updates.pop("type", None)
               or section_updates.pop("kind", None)
               or fm.get("type") or fm.get("kind") or "misc")
    key = str(section_updates.pop("key", None) or fm.get("key") or "unknown")

    def _pick(name, current):
        return section_updates.get(name, current)

    rendered = render_note(
        kind, key,
        title=_pick("title", parsed["title"] or key),
        resource=str(section_updates.get("resource",
                     _pick("source_url", fm.get("resource") or ""))),
        description=str(_pick("description", fm.get("description") or "")),
        objective=_pick("objective", sec.get("objective", "")),
        key_results=_pick("key_results", sec.get("key_results")),
        facts=_pick("facts", sec.get("facts")),
        links=_pick("links", sec.get("links")),
        learnings=_pick("learnings", sec.get("learnings")),
        body_md=_pick("body_md", parsed["body"]),
        timestamp=_now_iso(),      # the write IS the freshness event
        tags=_pick("tags", fm.get("tags")),
        # Provenance is carried through unchanged unless the caller replaces it:
        # a read-modify-write that dropped it would un-claim captures a peer is
        # waiting to archive.
        sources=_pick("sources", fm.get("sources")),
    )
    try:
        _atomic.write_text(path, rendered)
    except OSError as exc:
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
        # Cap: a brief's Learnings is an ever-growing audit trail; inject only
        # the most RECENT so it doesn't dominate (or evict Facts/body from) the
        # window. Full history stays on disk. Env AIFORGE_KNOWLEDGE_MAX_LEARNINGS.
        try:
            _kl = max(1, int(os.environ.get("AIFORGE_KNOWLEDGE_MAX_LEARNINGS", "12")))
        except (TypeError, ValueError):
            _kl = 12
        out.append("\n".join(f"- {l}" for l in learnings[-_kl:]))
    if parsed.get("body"):
        out.append(parsed["body"].strip())
    return "\n\n".join(out).strip() or (note or "").strip()
